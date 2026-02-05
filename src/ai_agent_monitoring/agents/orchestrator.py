"""Orchestrator Agent — Multi-Agentワークフローの制御."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from ai_agent_monitoring.agents.logs_agent import LogsAgent
from ai_agent_monitoring.agents.metrics_agent import MetricsAgent
from ai_agent_monitoring.agents.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from ai_agent_monitoring.agents.rca_agent import RCAAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import TriggerType
from ai_agent_monitoring.core.state import AgentState, EnvironmentContext, InvestigationPlan, TimeRange
from ai_agent_monitoring.tools.grafana import GrafanaMCPTool
from ai_agent_monitoring.tools.query_rag import get_query_rag
from ai_agent_monitoring.tools.query_validator import (
    QueryValidator,
    get_all_fewshot_examples,
)
from ai_agent_monitoring.tools.registry import ToolRegistry
from ai_agent_monitoring.tools.time import create_time_tools

if TYPE_CHECKING:
    from ai_agent_monitoring.tools.grafana import GrafanaMCPTool

logger = logging.getLogger(__name__)

# ステージ更新用コールバック型
StageUpdateCallback = Callable[[str, str, int | None], None]


class OrchestratorAgent:
    """Orchestrator Agent.

    アラートまたはユーザクエリを受け取り、調査計画の策定、
    Metrics/Logs Agentへの委任、RCAレポート生成までを制御する。
    """

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        settings: Settings | None = None,
        stage_update_callback: StageUpdateCallback | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or Settings()
        self.registry = registry
        self._stage_callback = stage_update_callback

        # 時刻ツールは常に利用可能
        self.time_tools = create_time_tools()

        # 各Agentはregistryから健全なMCPクライアントを使用
        # Grafana優先で、unhealthyなMCPはスキップ
        self.grafana_mcp = registry.grafana.client if registry.grafana.healthy else None
        prometheus_mcp = registry.prometheus.client if registry.prometheus.healthy else None
        loki_mcp = registry.loki.client if registry.loki.healthy else None

        # Grafana MCP Toolクラス（環境発見用）
        self.grafana_tool = GrafanaMCPTool(self.grafana_mcp) if self.grafana_mcp else None

        self.metrics_agent = MetricsAgent(
            llm,
            prometheus_mcp=prometheus_mcp,
            grafana_mcp=self.grafana_mcp,
        ) if prometheus_mcp or self.grafana_mcp else None

        self.logs_agent = LogsAgent(
            llm,
            loki_mcp=loki_mcp,
            grafana_mcp=self.grafana_mcp,
        ) if loki_mcp or self.grafana_mcp else None

        self.rca_agent = RCAAgent(llm, grafana_mcp=self.grafana_mcp)

        # クエリバリデータ
        self.query_validator = QueryValidator()

        self.graph = self._build_graph()

    def _update_stage(self, state: AgentState, stage: str) -> None:
        """調査ステージを更新."""
        inv_id = state.get("investigation_id", "")
        iteration = state.get("iteration_count", 0)
        if inv_id and self._stage_callback:
            self._stage_callback(inv_id, stage, iteration)

    def _wrap_with_stage(self, subgraph: Any, stage_name: str) -> Any:
        """サブグラフをステージ更新でラップ.

        サブグラフ（MetricsAgent, LogsAgent, RCAAgent）の実行前に
        ステージを更新するラッパー関数を返す。
        """
        async def wrapped(state: AgentState) -> dict[str, Any]:
            self._update_stage(state, stage_name)
            return await subgraph.ainvoke(state)
        return wrapped

    def _build_graph(self) -> StateGraph[AgentState]:
        """LangGraphワークフローを構築.

        利用可能なMCPに応じてグラフを動的に構築する。
        - Grafana MCP優先
        - unhealthyなMCPはスキップ
        - 最初に環境発見を行い、利用可能なメトリクス・ラベルを取得
        """
        graph = StateGraph(AgentState)

        # 基本ノード登録
        graph.add_node("discover_environment", self._discover_environment)
        graph.add_node("analyze_input", self._analyze_input)
        graph.add_node("plan_investigation", self._plan_investigation)
        graph.add_node("validate_queries", self._validate_queries)
        graph.add_node("resolve_time_range", self._resolve_time_range_node)
        graph.add_node("evaluate_results", self._evaluate_results)

        # エッジ定義
        graph.set_entry_point("discover_environment")
        graph.add_edge("discover_environment", "analyze_input")
        graph.add_edge("analyze_input", "plan_investigation")
        graph.add_edge("plan_investigation", "validate_queries")
        graph.add_edge("validate_queries", "resolve_time_range")

        # Metrics/Logs Agentは利用可能な場合のみ追加
        metrics_agent = self.metrics_agent
        logs_agent = self.logs_agent

        if metrics_agent is not None:
            graph.add_node("investigate_metrics", self._wrap_with_stage(
                metrics_agent.compile(),
                "メトリクスを調査中",
            ))
            graph.add_edge("resolve_time_range", "investigate_metrics")
            graph.add_edge("investigate_metrics", "evaluate_results")
        else:
            logger.warning("MetricsAgent unavailable, skipping metrics investigation")

        if logs_agent is not None:
            graph.add_node("investigate_logs", self._wrap_with_stage(
                logs_agent.compile(),
                "ログを調査中",
            ))
            graph.add_edge("resolve_time_range", "investigate_logs")
            graph.add_edge("investigate_logs", "evaluate_results")
        else:
            logger.warning("LogsAgent unavailable, skipping logs investigation")

        # 両方のAgentが使えない場合は直接評価へ
        if metrics_agent is None and logs_agent is None:
            graph.add_edge("resolve_time_range", "evaluate_results")
        graph.add_conditional_edges(
            "evaluate_results",
            self._should_continue,
            {
                "continue": "plan_investigation",
                "finish": "generate_rca",
            },
        )

        # RCA Agentをステージ更新でラップ
        graph.add_node("generate_rca", self._wrap_with_stage(
            self.rca_agent.compile(),
            "RCAレポートを生成中",
        ))
        graph.add_edge("generate_rca", END)

        return graph

    def compile(self) -> Any:
        """グラフをコンパイルして実行可能にする."""
        return self.graph.compile()

    # ---- ノード関数 ----

    async def _discover_environment(self, state: AgentState) -> dict[str, Any]:
        """環境情報を収集.

        Grafana MCP経由で利用可能なメトリクス・ラベル・ターゲットを取得し、
        調査計画の生成に必要なコンテキストを構築する。

        セッションを再利用して効率的にMCP呼び出しを行う。
        """
        self._update_stage(state, "環境情報を収集中")

        if not self.grafana_tool:
            logger.warning("Grafana MCP unavailable, skipping environment discovery")
            return {"environment": EnvironmentContext()}

        env = EnvironmentContext()

        try:
            # セッションを再利用して複数のMCP呼び出しを効率化
            async with self.grafana_tool.session_context() as grafana:
                await self._discover_datasources(grafana, env)
                await self._discover_prometheus_info(grafana, env)
                await self._discover_loki_info(grafana, env)
                await self._discover_dashboard_queries(grafana, env)
        except Exception as e:
            logger.warning(
                "Environment discovery failed: %s: %s",
                type(e).__name__,
                e,
            )

        return {"environment": env}

    async def _discover_datasources(
        self,
        grafana: "GrafanaMCPTool",
        env: EnvironmentContext,
    ) -> None:
        """データソース一覧を取得."""
        try:
            datasources_result = await grafana.list_datasources()
            datasources = self._extract_content_text(datasources_result)

            for ds in self._parse_datasources(datasources):
                if ds.get("type") == "prometheus" and not env.prometheus_datasource_uid:
                    env.prometheus_datasource_uid = ds.get("uid", "")
                    logger.info("Found Prometheus datasource: %s", env.prometheus_datasource_uid)
                elif ds.get("type") == "loki" and not env.loki_datasource_uid:
                    env.loki_datasource_uid = ds.get("uid", "")
                    logger.info("Found Loki datasource: %s", env.loki_datasource_uid)
        except Exception as e:
            logger.warning("Failed to list datasources: %s: %s", type(e).__name__, e)

    async def _discover_prometheus_info(
        self,
        grafana: "GrafanaMCPTool",
        env: EnvironmentContext,
    ) -> None:
        """Prometheusメトリクス・ラベル情報を取得."""
        if not env.prometheus_datasource_uid:
            return

        try:
            # メトリクス名一覧（上位100件）
            metrics_result = await grafana.list_prometheus_metric_names(
                env.prometheus_datasource_uid,
                limit=100,
            )
            env.available_metrics = self._extract_list_from_result(metrics_result)
            logger.info("Found %d Prometheus metrics", len(env.available_metrics))

            # ラベル名一覧
            labels_result = await grafana.list_prometheus_label_names(
                env.prometheus_datasource_uid,
            )
            env.available_labels = self._extract_list_from_result(labels_result)

            # jobラベルの値を取得
            if "job" in env.available_labels:
                jobs_result = await grafana.list_prometheus_label_values(
                    env.prometheus_datasource_uid,
                    "job",
                )
                env.available_jobs = self._extract_list_from_result(jobs_result)
                logger.info("Found %d jobs: %s", len(env.available_jobs), env.available_jobs[:5])

            # instanceラベルの値を取得
            if "instance" in env.available_labels:
                instances_result = await grafana.list_prometheus_label_values(
                    env.prometheus_datasource_uid,
                    "instance",
                )
                env.available_instances = self._extract_list_from_result(instances_result)
                logger.info("Found %d instances", len(env.available_instances))
        except Exception as e:
            logger.warning("Failed to get Prometheus info: %s: %s", type(e).__name__, e)

    async def _discover_loki_info(
        self,
        grafana: "GrafanaMCPTool",
        env: EnvironmentContext,
    ) -> None:
        """Lokiラベル情報を取得."""
        if not env.loki_datasource_uid:
            return

        try:
            loki_labels_result = await grafana.list_loki_label_names(env.loki_datasource_uid)
            env.loki_labels = self._extract_list_from_result(loki_labels_result)
            logger.info("Found %d Loki labels", len(env.loki_labels))

            if "job" in env.loki_labels:
                loki_jobs_result = await grafana.list_loki_label_values(
                    env.loki_datasource_uid,
                    "job",
                )
                env.loki_jobs = self._extract_list_from_result(loki_jobs_result)
        except Exception as e:
            logger.warning("Failed to get Loki info: %s: %s", type(e).__name__, e)

    async def _discover_dashboard_queries(
        self,
        grafana: "GrafanaMCPTool",
        env: EnvironmentContext,
    ) -> None:
        """既存ダッシュボードからクエリパターンを学習.

        複数のダッシュボードを試行し、有効なクエリが見つかるまで続ける。
        """
        try:
            dashboards_result = await grafana.list_dashboards()
            dashboards = self._extract_content_text(dashboards_result)
            dashboard_list = self._parse_dashboards(dashboards)

            # 最大3つのダッシュボードを試行
            for dashboard in dashboard_list[:3]:
                uid = dashboard.get("uid", "")
                if not uid:
                    continue

                try:
                    queries_result = await grafana.get_dashboard_panel_queries(uid)

                    # エラーレスポンスをチェック（パネルが無いダッシュボードなど）
                    if "error" in queries_result:
                        logger.debug(
                            "Skipping dashboard %s: %s",
                            uid,
                            queries_result.get("error"),
                        )
                        continue

                    queries_text = self._extract_content_text(queries_result)
                    promql, logql = self._extract_queries_from_panels(queries_text)

                    if promql or logql:
                        env.example_promql_queries = promql[:5]
                        env.example_logql_queries = logql[:5]
                        logger.info(
                            "Extracted %d PromQL, %d LogQL example queries from dashboard %s",
                            len(env.example_promql_queries),
                            len(env.example_logql_queries),
                            uid,
                        )
                        return  # 成功したら終了

                except Exception as panel_err:
                    logger.debug(
                        "Failed to get panel queries for dashboard %s: %s: %s",
                        uid,
                        type(panel_err).__name__,
                        panel_err,
                    )
                    continue

            # 全て失敗した場合
            logger.debug("No example queries found from any dashboard")

        except Exception as e:
            logger.warning("Failed to list dashboards: %s: %s", type(e).__name__, e)

    def _extract_content_text(self, result: dict[str, Any]) -> str:
        """MCPツール結果からテキストコンテンツを抽出."""
        content = result.get("content", [])
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts)

    def _extract_list_from_result(self, result: dict[str, Any]) -> list[str]:
        """MCPツール結果からリストを抽出."""
        text = self._extract_content_text(result)
        # JSON配列またはカンマ区切りリストをパース
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
        # 改行区切りで試行
        return [line.strip() for line in text.split("\n") if line.strip()]

    def _parse_datasources(self, text: str) -> list[dict[str, Any]]:
        """データソーステキストをパース."""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return []

    def _parse_dashboards(self, text: str) -> list[dict[str, Any]]:
        """ダッシュボードテキストをパース."""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return []

    def _extract_queries_from_panels(self, text: str) -> tuple[list[str], list[str]]:
        """パネルクエリテキストからPromQL/LogQLを抽出."""
        promql_queries: list[str] = []
        logql_queries: list[str] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                for panel in parsed:
                    expr = panel.get("expr", "") or panel.get("query", "")
                    if expr:
                        # LogQLは{で始まることが多い
                        if expr.strip().startswith("{"):
                            logql_queries.append(expr)
                        else:
                            promql_queries.append(expr)
        except json.JSONDecodeError:
            pass
        return promql_queries, logql_queries

    async def _analyze_input(self, state: AgentState) -> dict[str, Any]:
        """入力（アラートまたはユーザクエリ）を分析."""
        self._update_stage(state, "入力を分析中")

        user_query = state.get("user_query")
        query_text = ""  # RAG検索用

        if state["trigger_type"] == TriggerType.USER_QUERY and user_query is not None:
            query_text = user_query.raw_input
            content = (
                f"ユーザからの問い合わせ:\n{user_query.raw_input}\n\n"
                "この問い合わせ内容を分析し、何を調査すべきか整理してください。"
            )
        else:
            alert = state.get("alert")
            if alert is not None:
                query_text = f"{alert.alert_name} {alert.summary} {alert.description}"
                content = (
                    f"アラートを受信しました:\n"
                    f"名前: {alert.alert_name}\n"
                    f"重要度: {alert.severity}\n"
                    f"インスタンス: {alert.instance}\n"
                    f"概要: {alert.summary}\n"
                    f"詳細: {alert.description}\n\n"
                    "このアラートの調査方針を整理してください。"
                )
            else:
                content = "入力が不正です。アラートまたはユーザクエリが必要です。"

        # 現在時刻を取得してプロンプトに注入
        current_time = datetime.now(timezone.utc).isoformat()

        # 環境コンテキストをフォーマット
        env = state.get("environment")
        environment_context = self._format_environment_context(env)

        # RAGから関連ドキュメントを取得
        rag_context = self._get_rag_context(query_text)

        system_prompt = ORCHESTRATOR_SYSTEM_PROMPT.format(
            max_iterations=state.get("max_iterations", 3),
            current_time=current_time,
            environment_context=environment_context,
        )

        # RAGコンテキストがある場合はシステムプロンプトに追加
        if rag_context:
            system_prompt += f"\n\n## クエリリファレンス（関連ドキュメント）\n{rag_context}"

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]
        response = await self.llm.ainvoke(messages)

        return {"messages": [response]}

    def _get_rag_context(self, query: str, max_tokens: int = 1500) -> str:
        """RAGから関連ドキュメントを取得."""
        if not query:
            return ""

        try:
            rag = get_query_rag()
            return rag.get_relevant_context(query, max_tokens=max_tokens)
        except Exception as e:
            logger.warning("Failed to get RAG context: %s", e)
            return ""

    def _format_environment_context(self, env: EnvironmentContext | None) -> str:
        """環境コンテキストをプロンプト用テキストにフォーマット."""
        if env is None:
            return "環境情報は利用できません。"

        lines = []

        # データソースUID（クエリ実行時に必須）
        lines.append("### データソースUID（クエリ実行時に必須）")
        if env.prometheus_datasource_uid:
            lines.append(f"  - Prometheus: `{env.prometheus_datasource_uid}`")
        else:
            lines.append("  - Prometheus: 未設定")
        if env.loki_datasource_uid:
            lines.append(f"  - Loki: `{env.loki_datasource_uid}`")
        else:
            lines.append("  - Loki: 未設定")

        # Prometheusメトリクス情報
        if env.available_metrics:
            lines.append("### 利用可能なPrometheusメトリクス（一部）")
            for metric in env.available_metrics[:20]:
                lines.append(f"  - {metric}")
            if len(env.available_metrics) > 20:
                lines.append(f"  ... 他 {len(env.available_metrics) - 20} 件")

        # 利用可能なジョブ
        if env.available_jobs:
            lines.append("\n### 利用可能なjobラベル値")
            for job in env.available_jobs:
                lines.append(f"  - {job}")

        # 利用可能なインスタンス
        if env.available_instances:
            lines.append("\n### 利用可能なinstanceラベル値（一部）")
            for inst in env.available_instances[:10]:
                lines.append(f"  - {inst}")
            if len(env.available_instances) > 10:
                lines.append(f"  ... 他 {len(env.available_instances) - 10} 件")

        # Lokiラベル情報
        if env.loki_labels:
            lines.append("\n### 利用可能なLokiラベル")
            for label in env.loki_labels:
                lines.append(f"  - {label}")

        if env.loki_jobs:
            lines.append("\n### Lokiで利用可能なjobラベル値")
            for job in env.loki_jobs:
                lines.append(f"  - {job}")

        # 既存ダッシュボードからの例
        if env.example_promql_queries:
            lines.append("\n### 参考: 既存ダッシュボードのPromQLクエリ例")
            for q in env.example_promql_queries:
                lines.append(f"  - {q}")

        if env.example_logql_queries:
            lines.append("\n### 参考: 既存ダッシュボードのLogQLクエリ例")
            for q in env.example_logql_queries:
                lines.append(f"  - {q}")

        # Few-shot例を追加
        lines.append("\n" + get_all_fewshot_examples())

        if not lines:
            # 環境情報がなくてもFew-shot例は提供
            return get_all_fewshot_examples()

        return "\n".join(lines)

    async def _plan_investigation(self, state: AgentState) -> dict[str, Any]:
        """調査計画を策定."""
        iteration = state.get("iteration_count", 0) + 1
        self._update_stage(state, f"調査計画を策定中 (イテレーション {iteration})")

        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "上記の分析に基づき、調査計画をJSON形式で出力してください。\n"
                    "promql_queries, logql_queries, target_instances, time_range を含めてください。\n"
                    "time_rangeは必ずISO 8601絶対時刻のstart/endで指定してください。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        plan = self._parse_plan(response.content)

        # 環境コンテキストからdatasource UIDを自動設定
        env = state.get("environment")
        if env:
            if not plan.prometheus_datasource_uid and env.prometheus_datasource_uid:
                plan.prometheus_datasource_uid = env.prometheus_datasource_uid
            if not plan.loki_datasource_uid and env.loki_datasource_uid:
                plan.loki_datasource_uid = env.loki_datasource_uid

        return {
            "messages": [response],
            "plan": plan,
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    async def _validate_queries(self, state: AgentState) -> dict[str, Any]:
        """生成されたクエリを検証し、必要に応じて修正.

        LLMが生成したPromQL/LogQLクエリの文法を検証し、
        エラーがある場合は修正を試みるか、LLMに再生成を依頼する。
        """
        self._update_stage(state, "クエリを検証中")

        plan = state.get("plan")
        if not plan:
            return {}

        validation_errors: list[str] = []
        corrected_promql: list[str] = []
        corrected_logql: list[str] = []

        # PromQLの検証
        for query in plan.promql_queries:
            result = self.query_validator.validate_promql(query)
            if result.is_valid:
                corrected_promql.append(query)
            elif result.corrected_query:
                # 修正されたクエリを再検証
                revalidated = self.query_validator.validate_promql(
                    result.corrected_query
                )
                if revalidated.is_valid:
                    corrected_promql.append(result.corrected_query)
                    logger.info(
                        "PromQL auto-corrected: %s -> %s",
                        query, result.corrected_query
                    )
                else:
                    validation_errors.append(
                        f"PromQL: {query} - {', '.join(result.errors or [])}"
                    )
            else:
                validation_errors.append(
                    f"PromQL: {query} - {', '.join(result.errors or [])}"
                )

        # LogQLの検証
        for query in plan.logql_queries:
            result = self.query_validator.validate_logql(query)
            if result.is_valid:
                corrected_logql.append(query)
            elif result.corrected_query:
                # 修正されたクエリを再検証
                revalidated = self.query_validator.validate_logql(
                    result.corrected_query
                )
                if revalidated.is_valid:
                    corrected_logql.append(result.corrected_query)
                    logger.info(
                        "LogQL auto-corrected: %s -> %s",
                        query, result.corrected_query
                    )
                else:
                    validation_errors.append(
                        f"LogQL: {query} - {', '.join(result.errors or [])}"
                    )
            else:
                validation_errors.append(
                    f"LogQL: {query} - {', '.join(result.errors or [])}"
                )

        # エラーがあればLLMに再生成を依頼
        if validation_errors:
            logger.warning("Query validation errors: %s", validation_errors)

            # RAGから関連ドキュメントを取得
            error_keywords = " ".join(
                q.split(":")[0] for q in validation_errors
            )
            rag_context = self._get_rag_context(error_keywords, max_tokens=1000)

            # Few-shot例を含めて再生成を依頼
            fewshot = get_all_fewshot_examples()
            error_msg = "\n".join(validation_errors)

            retry_content = (
                f"生成されたクエリに文法エラーがありました:\n{error_msg}\n\n"
            )
            if rag_context:
                retry_content += f"参考ドキュメント:\n{rag_context}\n\n"
            retry_content += (
                f"以下のクエリ例を参考に、正しい文法でクエリを再生成してください:\n"
                f"{fewshot}\n\n"
                "修正した調査計画をJSON形式で出力してください。"
            )

            messages = [
                *state["messages"],
                HumanMessage(content=retry_content),
            ]
            response = await self.llm.ainvoke(messages)
            new_plan = self._parse_plan(response.content)

            # 再検証（再帰的に呼び出さない - 1回のみ）
            final_promql = []
            final_logql = []

            for query in new_plan.promql_queries:
                result = self.query_validator.validate_promql(query)
                if result.is_valid:
                    final_promql.append(query)
                elif result.corrected_query:
                    final_promql.append(result.corrected_query)
                else:
                    logger.error("PromQL still invalid after retry: %s", query)

            for query in new_plan.logql_queries:
                result = self.query_validator.validate_logql(query)
                if result.is_valid:
                    final_logql.append(query)
                elif result.corrected_query:
                    final_logql.append(result.corrected_query)
                else:
                    logger.error("LogQL still invalid after retry: %s", query)

            new_plan.promql_queries = final_promql
            new_plan.logql_queries = final_logql

            return {
                "messages": [response],
                "plan": new_plan,
            }

        # エラーがなければ修正済みクエリを適用
        plan.promql_queries = corrected_promql
        plan.logql_queries = corrected_logql

        return {"plan": plan}

    async def _resolve_time_range_node(self, state: AgentState) -> dict[str, Any]:
        """時間範囲を確定させるノード.

        LLMが調査計画でtime_rangeを出力できた場合はそのまま通過。
        できなかった場合:
        - Alert起動: アラート時刻から自動推定
        - ユーザクエリ起動: UserQueryの解析済み時間があればそれを使用、
          なければinterruptでユーザに問い合わせる
        """
        self._update_stage(state, "時間範囲を確定中")

        plan = state.get("plan")
        if not plan:
            return {}

        # LLMがtime_rangeを出力済みなら何もしない
        if plan.time_range is not None:
            return {}

        # Alert起動: アラート時刻から自動推定（人間の介入不要）
        alert = state.get("alert")
        if state["trigger_type"] == TriggerType.ALERT and alert is not None:
            alert_time = alert.starts_at
            plan.time_range = TimeRange(
                start=alert_time - timedelta(minutes=30),
                end=alert.ends_at or (alert_time + timedelta(minutes=30)),
            )
            return {"plan": plan}

        # ユーザクエリ: 解析済み時間範囲があればそれを使用
        user_query = state.get("user_query")
        if user_query:
            if user_query.time_range_start and user_query.time_range_end:
                plan.time_range = TimeRange(
                    start=user_query.time_range_start,
                    end=user_query.time_range_end,
                )
                return {"plan": plan}
            if user_query.time_range_start:
                plan.time_range = TimeRange(
                    start=user_query.time_range_start,
                    end=user_query.time_range_start + timedelta(hours=1),
                )
                return {"plan": plan}

        # ユーザクエリで時間範囲が不明 → ユーザに問い合わせ
        user_answer = interrupt(
            "調査対象の時間範囲を特定できませんでした。\n"
            "調査したい時間範囲を教えてください。\n"
            "例: 「昨日の16時から17時」「直近1時間」「2026-02-01 09:00 〜 10:00」"
        )

        # ユーザの回答をLLMでISO 8601に変換
        messages = [
            HumanMessage(
                content=(
                    f"ユーザが指定した時間範囲: 「{user_answer}」\n\n"
                    "この時間表現をISO 8601形式のstart/endに変換してJSON出力してください。\n"
                    '例: {{"start": "2026-02-01T16:00:00+09:00", "end": "2026-02-01T17:00:00+09:00"}}'
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        try:
            json_str = self._extract_json(response.content)
            data = json.loads(json_str)
            plan.time_range = TimeRange(
                start=datetime.fromisoformat(data["start"]),
                end=datetime.fromisoformat(data["end"]),
            )
        except (json.JSONDecodeError, ValueError, KeyError):
            # パース失敗時は最終フォールバック
            now = datetime.now(timezone.utc)
            logger.warning("ユーザ回答のパースに失敗。直近1時間をフォールバックとして使用。")
            plan.time_range = TimeRange(start=now - timedelta(hours=1), end=now)

        return {
            "messages": [response],
            "plan": plan,
        }

    async def _evaluate_results(self, state: AgentState) -> dict[str, Any]:
        """調査結果を評価し、追加調査が必要か判断."""
        self._update_stage(state, "調査結果を評価中")

        # Metrics/Logs Agentの結果サマリを構築
        results_summary = []
        for mr in state.get("metrics_results", []):
            results_summary.append(f"[メトリクス] {mr.summary}")
        for lr in state.get("logs_results", []):
            results_summary.append(f"[ログ] {lr.summary}")

        results_text = "\n".join(results_summary) if results_summary else "結果なし"

        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    f"各Agentの調査結果:\n{results_text}\n\n"
                    "根本原因を特定するのに十分な情報がありますか？\n"
                    "回答は 'SUFFICIENT' または 'INSUFFICIENT' で始めてください。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        first_line = response.content.upper().split("\n")[0]
        is_complete = first_line.startswith("SUFFICIENT")

        return {
            "messages": [response],
            "investigation_complete": is_complete,
        }

    # ---- ルーティング ----

    def _should_continue(self, state: AgentState) -> str:
        """追加調査を続けるかRCA生成に進むか判断."""
        if state.get("investigation_complete"):
            return "finish"
        if state.get("iteration_count", 0) >= state.get("max_iterations", 3):
            logger.warning("最大イテレーション数(%d)に到達。RCA生成に移行。", state.get("max_iterations", 3))
            return "finish"
        return "continue"

    # ---- パーサー ----

    def _parse_plan(self, content: str) -> InvestigationPlan:
        """LLM出力から調査計画をパース.

        パースに失敗した場合は例外を発生させ、デフォルト計画にフォールバックしない。
        これにより、意味のない調査が実行されることを防ぐ。

        Raises:
            ValueError: 調査計画のパースに失敗した場合
        """
        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            return InvestigationPlan(**data)
        except (json.JSONDecodeError, ValueError) as e:
            # エラーの詳細をログ出力
            logger.error(
                "調査計画のパースに失敗。error=%s, content_preview=%s",
                e,
                content[:500] if content else "(empty)",
            )
            raise ValueError(f"調査計画のパースに失敗しました: {e}") from e

    @staticmethod
    def _format_time_range(time_range: TimeRange | None) -> str:
        """TimeRangeを人間可読な文字列に変換."""
        if time_range is None:
            return "指定なし"
        return f"{time_range.start.isoformat()} 〜 {time_range.end.isoformat()}"

    @staticmethod
    def _extract_json(text: str) -> str:
        """テキストからJSON部分を抽出."""
        # ```json ... ``` 形式を優先
        if "```json" in text:
            try:
                start = text.index("```json") + 7
                end = text.index("```", start)
                return text[start:end].strip()
            except ValueError:
                pass  # フォールバックへ

        # ``` ... ``` 形式（言語指定なし）
        if "```" in text:
            try:
                start = text.index("```") + 3
                # 改行をスキップ
                while start < len(text) and text[start] in "\n\r":
                    start += 1
                end = text.index("```", start)
                candidate = text[start:end].strip()
                if candidate.startswith("{"):
                    return candidate
            except ValueError:
                pass

        # 生の{...}を探す
        if "{" in text and "}" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            return text[start:end]

        # JSONが見つからない
        raise ValueError(f"No JSON found in text: {text[:200]}...")
