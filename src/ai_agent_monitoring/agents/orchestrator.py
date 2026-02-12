"""Orchestrator Agent — Multi-Agentワークフローの制御."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from ai_agent_monitoring.agents.logs_agent import LogsAgent
from ai_agent_monitoring.agents.metrics_agent import MetricsAgent
from ai_agent_monitoring.agents.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from ai_agent_monitoring.agents.rca_agent import RCAAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import TriggerType
from ai_agent_monitoring.core.sanitizer import sanitize_user_input
from ai_agent_monitoring.core.state import (
    AgentState,
    DashboardInfo,
    EnvironmentContext,
    EvaluationFeedback,
    InvestigationPlan,
    PanelQuery,
    TimeRange,
)
from ai_agent_monitoring.tools.grafana import GrafanaMCPTool
from ai_agent_monitoring.tools.query_rag import get_query_rag
from ai_agent_monitoring.tools.query_validator import (
    QueryType,
    QueryValidator,
    get_all_fewshot_examples,
)
from ai_agent_monitoring.tools.registry import ToolRegistry
from ai_agent_monitoring.tools.time import create_time_tools

if TYPE_CHECKING:
    from langgraph.pregel import Pregel

    from ai_agent_monitoring.tools.grafana import GrafanaMCPTool

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe
    _LANGFUSE_OBSERVE_AVAILABLE = True
except ImportError:
    _LANGFUSE_OBSERVE_AVAILABLE = False

    def _observe(
        func: Any = None, **kwargs: Any
    ) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func
        return lambda f: f

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

        # サブエージェントの compile() 結果をキャッシュ
        # StateGraph.compile() は比較的重い処理のため、初回のみ実行して再利用する
        self._compiled_metrics: Pregel[Any] | None = (
            self.metrics_agent.compile() if self.metrics_agent is not None else None
        )
        self._compiled_logs: Pregel[Any] | None = (
            self.logs_agent.compile() if self.logs_agent is not None else None
        )
        self._compiled_rca: Pregel[Any] = self.rca_agent.compile()

        # クエリバリデータ
        self.query_validator = QueryValidator()

        self.graph = self._build_graph()

    def _update_stage(self, state: AgentState, stage: str) -> None:
        """調査ステージを更新."""
        inv_id = state.get("investigation_id", "")
        iteration = state.get("iteration_count", 0)
        if inv_id and self._stage_callback:
            self._stage_callback(inv_id, stage, iteration)

    def _wrap_with_stage(self, subgraph: Pregel[Any], stage_name: str) -> Any:
        """サブグラフをステージ更新でラップ.

        サブグラフ（MetricsAgent, LogsAgent, RCAAgent）の実行前に
        ステージを更新するラッパー関数を返す。
        LangGraphの config（LangfuseCallbackHandler含む）を
        サブグラフに伝播させる。
        """
        async def wrapped(state: AgentState, config: RunnableConfig | None = None) -> dict[str, Any]:
            self._update_stage(state, stage_name)
            result: dict[str, Any] = await subgraph.ainvoke(state, config=config)
            return result
        return wrapped

    def _build_graph(self) -> StateGraph[AgentState]:
        """LangGraphワークフローを構築.

        利用可能なMCPに応じてグラフを動的に構築する。
        - Grafana MCP優先
        - unhealthyなMCPはスキップ
        - 最初に環境発見を行い、利用可能なメトリクス・ラベルを取得

        並列実行:
        resolve_time_range からの fan-out で investigate_metrics と
        investigate_logs を並列実行し、evaluate_results で fan-in する。
        LangGraphは1つのノードから複数のadd_edgeがある場合、
        自動的に並列実行し、合流先ノードは全入力の到着を待つ。
        """
        graph = StateGraph(AgentState)

        # 基本ノード登録
        graph.add_node("discover_environment", self._discover_environment)
        graph.add_node("analyze_input", self._analyze_input)
        graph.add_node("plan_investigation", self._plan_investigation)
        graph.add_node("validate_queries", self._validate_queries)
        graph.add_node("resolve_time_range", self._resolve_time_range_node)
        graph.add_node("evaluate_results", self._evaluate_results)

        # エッジ定義（直列部分）
        graph.set_entry_point("discover_environment")
        graph.add_edge("discover_environment", "analyze_input")
        graph.add_edge("analyze_input", "plan_investigation")
        graph.add_edge("plan_investigation", "validate_queries")
        graph.add_edge("validate_queries", "resolve_time_range")

        # 並列実行: resolve_time_range -> [investigate_metrics, investigate_logs] -> evaluate_results
        # LangGraphのfan-out/fan-inパターンを活用:
        # - fan-out: resolve_time_range から両方のサブエージェントに分岐
        # - fan-in: 両方が完了後に evaluate_results に合流
        # - 片方のみ利用可能な場合は、利用可能なAgentのみ実行される
        compiled_metrics = self._compiled_metrics
        compiled_logs = self._compiled_logs

        if compiled_metrics is not None:
            graph.add_node("investigate_metrics", self._wrap_with_stage(
                compiled_metrics,
                "メトリクスを調査中",
            ))
            graph.add_edge("resolve_time_range", "investigate_metrics")
            graph.add_edge("investigate_metrics", "evaluate_results")
        else:
            logger.warning("MetricsAgent unavailable, skipping metrics investigation")

        if compiled_logs is not None:
            graph.add_node("investigate_logs", self._wrap_with_stage(
                compiled_logs,
                "ログを調査中",
            ))
            graph.add_edge("resolve_time_range", "investigate_logs")
            graph.add_edge("investigate_logs", "evaluate_results")
        else:
            logger.warning("LogsAgent unavailable, skipping logs investigation")

        # 両方のAgentが使えない場合は直接評価へ
        if compiled_metrics is None and compiled_logs is None:
            graph.add_edge("resolve_time_range", "evaluate_results")
        graph.add_conditional_edges(
            "evaluate_results",
            self._should_continue,
            {
                "continue": "plan_investigation",
                "finish": "generate_rca",
            },
        )

        # RCA Agentをステージ更新でラップ（キャッシュ済みコンパイル結果を使用）
        graph.add_node("generate_rca", self._wrap_with_stage(
            self._compiled_rca,
            "RCAレポートを生成中",
        ))
        graph.add_edge("generate_rca", END)

        return graph

    def compile(self) -> Any:
        """グラフをコンパイルして実行可能にする."""
        return self.graph.compile()

    # ---- ノード関数 ----

    def _extract_investigation_keywords(self, state: AgentState) -> list[str]:
        """調査対象からキーワードを抽出.

        ユーザクエリやアラートから、ダッシュボード検索に使用する
        キーワードを抽出する。
        """
        keywords: set[str] = set()

        # 一般的な監視キーワードのマッピング
        keyword_map = {
            # CPU関連
            "cpu": ["cpu", "processor", "compute"],
            "プロセッサ": ["cpu", "processor"],
            "使用率": ["usage", "utilization"],
            # メモリ関連
            "memory": ["memory", "ram", "mem"],
            "メモリ": ["memory", "ram", "mem"],
            # ディスク関連
            "disk": ["disk", "storage", "filesystem"],
            "ディスク": ["disk", "storage", "filesystem"],
            "ストレージ": ["disk", "storage"],
            # ネットワーク関連
            "network": ["network", "net", "traffic"],
            "ネットワーク": ["network", "net", "traffic"],
            "通信": ["network", "traffic"],
            # コンテナ/Kubernetes関連
            "container": ["container", "docker", "pod"],
            "コンテナ": ["container", "docker", "pod"],
            "kubernetes": ["kubernetes", "k8s", "kube"],
            "pod": ["pod", "kubernetes"],
            # ノード関連
            "node": ["node", "host", "server"],
            "ノード": ["node", "host"],
            "サーバ": ["server", "host", "node"],
            "サーバー": ["server", "host", "node"],
            # エラー/障害関連
            "error": ["error", "alert", "failure"],
            "エラー": ["error", "alert", "failure"],
            "障害": ["error", "failure", "down"],
            # ログ関連
            "log": ["log", "logs", "logging"],
            "ログ": ["log", "logs"],
        }

        # 入力テキストを取得
        input_text = ""
        if state["trigger_type"] == TriggerType.USER_QUERY:
            user_query = state.get("user_query")
            if user_query:
                input_text = user_query.raw_input.lower()
        else:
            alert = state.get("alert")
            if alert:
                input_text = " ".join([
                    alert.alert_name or "",
                    alert.summary or "",
                    alert.description or "",
                ]).lower()

        # キーワードマッチング
        for key, values in keyword_map.items():
            if key.lower() in input_text:
                keywords.update(values)

        # 入力テキストから直接抽出（英数字の単語）
        import re
        words = re.findall(r"[a-zA-Z]{3,}", input_text)
        for word in words:
            if len(word) >= 3:
                keywords.add(word.lower())

        return list(keywords)

    @_observe(name="discover_environment", as_type="span")
    async def _discover_environment(self, state: AgentState) -> dict[str, Any]:
        """環境情報を収集.

        1. 入力からキーワードを抽出
        2. Grafana MCP経由でデータソース・メトリクス情報を取得
        3. キーワードに関連するダッシュボードを優先的に探索

        セッションを再利用して効率的にMCP呼び出しを行う。
        """
        self._update_stage(state, "環境情報を収集中")

        if not self.grafana_tool:
            logger.warning("Grafana MCP unavailable, skipping environment discovery")
            return {"environment": EnvironmentContext()}

        env = EnvironmentContext()

        # 入力からキーワードを抽出
        env.investigation_keywords = self._extract_investigation_keywords(state)
        if env.investigation_keywords:
            logger.info("Investigation keywords: %s", env.investigation_keywords)

        try:
            # セッションを再利用して複数のMCP呼び出しを効率化
            async with self.grafana_tool.session_context() as grafana:
                await self._discover_datasources(grafana, env)
                await self._discover_prometheus_info(grafana, env)
                await self._discover_loki_info(grafana, env)
                # キーワードを使ってダッシュボードを探索
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
        grafana: GrafanaMCPTool,
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
        grafana: GrafanaMCPTool,
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
        grafana: GrafanaMCPTool,
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

    async def _discover_dashboards(
        self,
        grafana: GrafanaMCPTool,
        env: EnvironmentContext,
    ) -> None:
        """ダッシュボード一覧を取得してEnvironmentContextに格納.

        キーワードマッチングによる関連度スコアは後続の処理で計算する。
        """
        try:
            dashboards_result = await grafana.list_dashboards()
            dashboards = self._extract_content_text(dashboards_result)
            dashboard_list = self._parse_dashboards(dashboards)

            for db in dashboard_list:
                uid = db.get("uid", "")
                if not uid:
                    continue
                env.available_dashboards.append(
                    DashboardInfo(
                        uid=uid,
                        title=db.get("title", ""),
                        tags=db.get("tags", []),
                    )
                )

            logger.info("Found %d dashboards", len(env.available_dashboards))
        except Exception as e:
            logger.warning("Failed to list dashboards: %s: %s", type(e).__name__, e)

    def _score_dashboard_relevance(
        self,
        dashboard: DashboardInfo,
        keywords: list[str],
    ) -> float:
        """ダッシュボードとキーワードの関連度スコアを計算.

        タイトルとタグにキーワードが含まれているかをチェック。
        """
        if not keywords:
            return 0.0

        score = 0.0
        title_lower = dashboard.title.lower()
        tags_lower = [t.lower() for t in dashboard.tags]

        for keyword in keywords:
            kw_lower = keyword.lower()
            # タイトルに含まれる場合は高スコア
            if kw_lower in title_lower:
                score += 2.0
            # タグに含まれる場合も加点
            for tag in tags_lower:
                if kw_lower in tag:
                    score += 1.0

        # キーワード数で正規化
        return score / len(keywords) if keywords else 0.0

    @_observe(name="rank_dashboards_by_keywords", as_type="span")
    def _rank_dashboards_by_keywords(
        self,
        dashboards: list[DashboardInfo],
        keywords: list[str],
    ) -> list[DashboardInfo]:
        """キーワードマッチングでダッシュボードをランキング.

        関連度スコアの高い順にソート。スコア0のダッシュボードも
        末尾に含める（フォールバック用）。
        """
        for db in dashboards:
            db.relevance_score = self._score_dashboard_relevance(db, keywords)

        # スコア降順でソート
        return sorted(dashboards, key=lambda d: d.relevance_score, reverse=True)

    async def _discover_panel_queries_from_dashboard(
        self,
        grafana: GrafanaMCPTool,
        dashboard: DashboardInfo,
        env: EnvironmentContext,
    ) -> bool:
        """指定ダッシュボードからパネルクエリを取得.

        Returns:
            クエリが見つかった場合はTrue
        """
        if dashboard.uid in env.explored_dashboard_uids:
            return False

        env.explored_dashboard_uids.append(dashboard.uid)

        try:
            queries_result = await grafana.get_dashboard_panel_queries(dashboard.uid)

            if "error" in queries_result:
                logger.debug(
                    "Skipping dashboard %s (%s): %s",
                    dashboard.uid,
                    dashboard.title,
                    queries_result.get("error"),
                )
                return False

            queries_text = self._extract_content_text(queries_result)
            panels = self._parse_panel_queries(queries_text, dashboard)

            if panels:
                env.discovered_panel_queries.extend(panels)
                logger.info(
                    "Extracted %d queries from dashboard '%s' (%s)",
                    len(panels),
                    dashboard.title,
                    dashboard.uid,
                )
                return True

        except Exception as e:
            logger.debug(
                "Failed to get panel queries for dashboard %s (%s): %s: %s",
                dashboard.uid,
                dashboard.title,
                type(e).__name__,
                e,
            )

        return False

    def _parse_panel_queries(
        self,
        text: str,
        dashboard: DashboardInfo,
    ) -> list[PanelQuery]:
        """パネルクエリテキストをPanelQueryリストに変換."""
        queries: list[PanelQuery] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                for panel in parsed:
                    expr = panel.get("expr", "") or panel.get("query", "")
                    if not expr:
                        continue

                    # LogQLは{で始まることが多い
                    query_type = "logql" if expr.strip().startswith("{") else "promql"
                    queries.append(
                        PanelQuery(
                            panel_title=panel.get("title", ""),
                            query=expr,
                            query_type=query_type,
                            dashboard_uid=dashboard.uid,
                            dashboard_title=dashboard.title,
                        )
                    )
        except json.JSONDecodeError:
            pass
        return queries

    async def _discover_dashboard_queries(
        self,
        grafana: GrafanaMCPTool,
        env: EnvironmentContext,
        keywords: list[str] | None = None,
        max_dashboards: int = 5,
    ) -> None:
        """キーワードに関連するダッシュボードからクエリを探索.

        1. キーワードでダッシュボードをランキング
        2. 関連度の高いダッシュボードから順にパネルを探索
        3. クエリが見つかるまで、または最大数に達するまで継続

        Args:
            grafana: Grafana MCPツール
            env: 環境コンテキスト
            keywords: 調査キーワード（省略時はenv.investigation_keywordsを使用）
            max_dashboards: 探索する最大ダッシュボード数
        """
        # ダッシュボード一覧がなければ取得
        if not env.available_dashboards:
            await self._discover_dashboards(grafana, env)

        if not env.available_dashboards:
            logger.debug("No dashboards available")
            return

        # キーワードでランキング
        search_keywords = keywords or env.investigation_keywords
        ranked = self._rank_dashboards_by_keywords(
            env.available_dashboards,
            search_keywords,
        )

        if search_keywords:
            logger.info(
                "Searching dashboards with keywords: %s",
                search_keywords,
            )

        # 上位ダッシュボードを探索
        explored_count = 0
        for dashboard in ranked:
            if explored_count >= max_dashboards:
                break

            # 既に探索済みならスキップ
            if dashboard.uid in env.explored_dashboard_uids:
                continue

            found = await self._discover_panel_queries_from_dashboard(
                grafana,
                dashboard,
                env,
            )
            explored_count += 1

            # クエリが見つかったら、例としてEnvironmentContextに設定
            if found and not env.example_promql_queries:
                promql = [
                    q.query for q in env.discovered_panel_queries
                    if q.query_type == "promql"
                ]
                logql = [
                    q.query for q in env.discovered_panel_queries
                    if q.query_type == "logql"
                ]
                env.example_promql_queries = promql[:5]
                env.example_logql_queries = logql[:5]

        if not env.discovered_panel_queries:
            logger.debug(
                "No panel queries found after exploring %d dashboards",
                explored_count,
            )

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

    @_observe(name="analyze_input", as_type="span")
    async def _analyze_input(self, state: AgentState) -> dict[str, Any]:
        """入力（アラートまたはユーザクエリ）を分析."""
        self._update_stage(state, "入力を分析中")

        user_query = state.get("user_query")
        query_text = ""  # RAG検索用

        if state["trigger_type"] == TriggerType.USER_QUERY and user_query is not None:
            query_text = user_query.raw_input
            sanitized_input = sanitize_user_input(user_query.raw_input)
            content = (
                f"ユーザからの問い合わせ:\n{sanitized_input}\n\n"
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
        current_time = datetime.now(UTC).isoformat()

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
            lines.append(
                "  - Prometheus: (grafana_list_datasources で取得してください)"
            )
        if env.loki_datasource_uid:
            lines.append(f"  - Loki: `{env.loki_datasource_uid}`")
        else:
            lines.append(
                "  - Loki: (grafana_list_datasources で取得してください)"
            )

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

    @_observe(name="plan_investigation", as_type="span")
    async def _plan_investigation(self, state: AgentState) -> dict[str, Any]:
        """調査計画を策定.

        前回のイテレーションでINSUFFICIENTと評価された場合、
        評価フィードバック（不足情報、追加調査観点、既試行クエリ）を
        プロンプトに含め、同じクエリの繰り返しを防止する。
        """
        iteration = state.get("iteration_count", 0) + 1
        self._update_stage(state, f"調査計画を策定中 (イテレーション {iteration})")

        # 基本プロンプト
        plan_prompt = (
            "上記の分析に基づき、調査計画をJSON形式で出力してください。\n"
            "promql_queries, logql_queries, target_instances, time_range を含めてください。\n"
            "time_rangeは必ずISO 8601絶対時刻のstart/endで指定してください。"
        )

        # 前回の評価フィードバックがある場合、プロンプトに追加
        feedback = state.get("evaluation_feedback")
        if feedback is not None:
            feedback_sections = []

            if feedback.missing_information:
                feedback_sections.append(
                    "## 前回の調査で不足していた情報\n"
                    + "\n".join(f"- {item}" for item in feedback.missing_information)
                )

            if feedback.additional_investigation_points:
                feedback_sections.append(
                    "## 追加で調査すべき観点\n"
                    + "\n".join(
                        f"- {point}" for point in feedback.additional_investigation_points
                    )
                )

            if feedback.previous_queries_attempted:
                feedback_sections.append(
                    "## 前回試行済みのクエリ（同じクエリは避けてください）\n"
                    + "\n".join(
                        f"- `{q}`" for q in feedback.previous_queries_attempted
                    )
                )

            if feedback.reasoning:
                feedback_sections.append(
                    f"## 前回の評価理由\n{feedback.reasoning}"
                )

            if feedback_sections:
                plan_prompt = (
                    "\n\n".join(feedback_sections)
                    + "\n\n上記のフィードバックを踏まえ、前回とは異なるアプローチで"
                    "調査計画をJSON形式で出力してください。\n"
                    "promql_queries, logql_queries, target_instances, time_range を含めてください。\n"
                    "time_rangeは必ずISO 8601絶対時刻のstart/endで指定してください。"
                )

        messages = [
            *state["messages"],
            HumanMessage(content=plan_prompt),
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

    @_observe(name="validate_queries", as_type="span")
    async def _validate_queries(self, state: AgentState) -> dict[str, Any]:
        """生成されたクエリを検証し、必要に応じて修正.

        LLMが生成したPromQL/LogQLクエリの文法を検証し、
        エラーがある場合は修正を試みるか、LLMに再生成を依頼する。

        検証項目:
        - datasource_uid の有効性
        - クエリ構文（PromQL/LogQL）
        - Grafana変数の検出（警告）
        - 二重ブレースの修正
        """
        self._update_stage(state, "クエリを検証中")

        plan = state.get("plan")
        if not plan:
            return {}

        env = state.get("environment")
        validation_errors: list[str] = []
        corrected_promql: list[str] = []
        corrected_logql: list[str] = []

        # datasource_uid の検証と自動修正
        if not self.query_validator.is_valid_datasource_uid(plan.prometheus_datasource_uid):
            if env and env.prometheus_datasource_uid:
                logger.info(
                    "Invalid prometheus_datasource_uid '%s', using env value: %s",
                    plan.prometheus_datasource_uid,
                    env.prometheus_datasource_uid,
                )
                plan.prometheus_datasource_uid = env.prometheus_datasource_uid
            else:
                logger.warning(
                    "Invalid prometheus_datasource_uid and no env fallback: %s",
                    plan.prometheus_datasource_uid,
                )

        if not self.query_validator.is_valid_datasource_uid(plan.loki_datasource_uid):
            if env and env.loki_datasource_uid:
                logger.info(
                    "Invalid loki_datasource_uid '%s', using env value: %s",
                    plan.loki_datasource_uid,
                    env.loki_datasource_uid,
                )
                plan.loki_datasource_uid = env.loki_datasource_uid
            else:
                logger.warning(
                    "Invalid loki_datasource_uid and no env fallback: %s",
                    plan.loki_datasource_uid,
                )

        # PromQLの検証
        for query in plan.promql_queries:
            # サニタイズ（二重ブレース、Grafana変数）
            sanitized, warnings = self.query_validator.sanitize_query(
                query, QueryType.PROMQL
            )
            for w in warnings:
                logger.warning("PromQL sanitize warning: %s - %s", query, w)

            # Grafana変数を含む場合はスキップ
            if self.query_validator.contains_grafana_variables(sanitized):
                logger.info("Skipping query with Grafana variables: %s", sanitized)
                continue

            result = self.query_validator.validate_promql(sanitized)
            if result.is_valid:
                corrected_promql.append(sanitized)
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
            # サニタイズ（二重ブレース、Grafana変数）
            sanitized, warnings = self.query_validator.sanitize_query(
                query, QueryType.LOGQL
            )
            for w in warnings:
                logger.warning("LogQL sanitize warning: %s - %s", query, w)

            # Grafana変数を含む場合はスキップ
            if self.query_validator.contains_grafana_variables(sanitized):
                logger.info("Skipping query with Grafana variables: %s", sanitized)
                continue

            result = self.query_validator.validate_logql(sanitized)
            if result.is_valid:
                corrected_logql.append(sanitized)
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
            now = datetime.now(UTC)
            logger.warning("ユーザ回答のパースに失敗。直近1時間をフォールバックとして使用。")
            plan.time_range = TimeRange(start=now - timedelta(hours=1), end=now)

        return {
            "messages": [response],
            "plan": plan,
        }

    @_observe(name="evaluate_results", as_type="span")
    async def _evaluate_results(self, state: AgentState) -> dict[str, Any]:
        """調査結果を評価し、追加調査が必要か判断.

        INSUFFICIENTの場合は、不足情報と追加調査観点を構造化して
        stateに保存し、次のイテレーションで同じクエリの繰り返しを防止する。
        """
        self._update_stage(state, "調査結果を評価中")

        # Metrics/Logs Agentの結果サマリを構築
        results_summary = []
        for mr in state.get("metrics_results", []):
            results_summary.append(f"[メトリクス] {mr.summary}")
        for lr in state.get("logs_results", []):
            results_summary.append(f"[ログ] {lr.summary}")

        results_text = "\n".join(results_summary) if results_summary else "結果なし"

        # これまでに試行したクエリを収集
        plan = state.get("plan")
        previous_queries: list[str] = []
        if plan:
            previous_queries.extend(plan.promql_queries)
            previous_queries.extend(plan.logql_queries)

        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    f"各Agentの調査結果:\n{results_text}\n\n"
                    "根本原因を特定するのに十分な情報がありますか？\n"
                    "回答は 'SUFFICIENT' または 'INSUFFICIENT' で始めてください。\n\n"
                    "INSUFFICIENTの場合は、以下のJSON形式で不足情報を出力してください:\n"
                    "```json\n"
                    "{\n"
                    '  "missing_information": ["不足している情報1", "不足している情報2"],\n'
                    '  "additional_investigation_points": ["追加で調査すべき観点1", "追加で調査すべき観点2"],\n'
                    '  "reasoning": "判定理由の説明"\n'
                    "}\n"
                    "```"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        first_line = response.content.upper().split("\n")[0]
        is_complete = first_line.startswith("SUFFICIENT")

        result: dict[str, Any] = {
            "messages": [response],
            "investigation_complete": is_complete,
        }

        # INSUFFICIENTの場合、構造化されたフィードバックを抽出してstateに保存
        if not is_complete:
            feedback = self._parse_evaluation_feedback(
                response.content, previous_queries
            )
            result["evaluation_feedback"] = feedback
            logger.info(
                "Evaluation: INSUFFICIENT - missing=%s, additional_points=%s",
                feedback.missing_information,
                feedback.additional_investigation_points,
            )

        return result

    def _parse_evaluation_feedback(
        self,
        content: str,
        previous_queries: list[str],
    ) -> EvaluationFeedback:
        """LLMの評価結果からEvaluationFeedbackをパース."""
        feedback = EvaluationFeedback(
            previous_queries_attempted=previous_queries,
        )

        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            feedback.missing_information = data.get("missing_information", [])
            feedback.additional_investigation_points = data.get(
                "additional_investigation_points", []
            )
            feedback.reasoning = data.get("reasoning", "")
        except (ValueError, json.JSONDecodeError):
            # JSONパースに失敗した場合、テキスト全体をreasoningとして保持
            lines = content.split("\n")
            # 最初の行（INSUFFICIENT）を除いたテキストを理由として使用
            feedback.reasoning = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

        return feedback

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

            # time_rangeの正規化: LLMが様々な形式で出力する可能性に対応
            if "time_range" in data and data["time_range"] is not None:
                tr = data["time_range"]
                if isinstance(tr, str):
                    # 単一の文字列の場合はNoneに（後続の処理で解決）
                    logger.debug("time_range is string, setting to None: %s", tr)
                    data["time_range"] = None
                elif isinstance(tr, dict):
                    # start/endを持つdictの場合はTimeRangeに変換
                    if "start" in tr and "end" in tr:
                        try:
                            data["time_range"] = TimeRange(
                                start=datetime.fromisoformat(str(tr["start"])),
                                end=datetime.fromisoformat(str(tr["end"])),
                            )
                        except (ValueError, TypeError) as parse_err:
                            logger.debug(
                                "Failed to parse time_range dict, setting to None: %s",
                                parse_err,
                            )
                            data["time_range"] = None
                    else:
                        data["time_range"] = None

            return InvestigationPlan(**data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
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
