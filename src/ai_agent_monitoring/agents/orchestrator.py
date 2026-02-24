"""Orchestrator Agent — Multi-Agentワークフローの制御."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, cast

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

    def _observe(func: Any = None, **kwargs: Any) -> Any:
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

        # クエリバリデータ
        self.query_validator = QueryValidator()

        # サブエージェント生成+グラフ構築
        self._rebuild_agents_and_graph()

    def _rebuild_agents_and_graph(self) -> None:
        """registryの健全性に基づきサブエージェントとグラフを再構築."""
        registry = self.registry

        # 各Agentはregistryから健全なMCPクライアントを使用
        # Grafana優先で、unhealthyなMCPはスキップ
        self.grafana_mcp = registry.grafana.client if registry.grafana.healthy else None
        prometheus_mcp = registry.prometheus.client if registry.prometheus.healthy else None
        loki_mcp = registry.loki.client if registry.loki.healthy else None

        # Grafana MCP Toolクラス（環境発見用）
        self.grafana_tool = GrafanaMCPTool(self.grafana_mcp) if self.grafana_mcp else None

        self.metrics_agent = (
            MetricsAgent(
                self.llm,
                prometheus_mcp=prometheus_mcp,
                grafana_mcp=self.grafana_mcp,
            )
            if prometheus_mcp or self.grafana_mcp
            else None
        )

        self.logs_agent = (
            LogsAgent(
                self.llm,
                loki_mcp=loki_mcp,
                grafana_mcp=self.grafana_mcp,
            )
            if loki_mcp or self.grafana_mcp
            else None
        )

        self.rca_agent = RCAAgent(self.llm, grafana_mcp=self.grafana_mcp)

        # サブエージェントの compile() 結果をキャッシュ
        self._compiled_metrics: Pregel[Any] | None = (
            self.metrics_agent.compile() if self.metrics_agent is not None else None
        )
        self._compiled_logs: Pregel[Any] | None = self.logs_agent.compile() if self.logs_agent is not None else None
        self._compiled_rca: Pregel[Any] = self.rca_agent.compile()

        self.graph = self._build_graph()

    def refresh_health(self, registry: ToolRegistry) -> dict[str, bool]:
        """registryを更新しグラフを再構築、各MCPの健全性を返す.

        調査実行前に呼び出し、MCPの状態変化をグラフ構造に反映する。
        """
        self.registry = registry
        self._rebuild_agents_and_graph()
        return {conn.name: conn.healthy for conn in [registry.prometheus, registry.loki, registry.grafana]}

    def _update_stage(self, state: AgentState, stage: str) -> None:
        """調査ステージを更新."""
        inv_id = state.get("investigation_id", "")
        iteration = state.get("iteration_count", 0)
        if inv_id and self._stage_callback:
            self._stage_callback(inv_id, stage, iteration)

    def _wrap_with_stage(
        self,
        subgraph: Pregel[Any],
        stage_name: str,
        output_keys: frozenset[str] | None = None,
    ) -> Any:
        """サブグラフをステージ更新でラップ.

        サブグラフ（MetricsAgent, LogsAgent, RCAAgent）の実行前に
        ステージを更新するラッパー関数を返す。
        LangGraphの config（LangfuseCallbackHandler含む）を
        サブグラフに伝播させる。

        Args:
            subgraph: コンパイル済みサブグラフ
            stage_name: ステージ表示名
            output_keys: 返却するステートキーのセット。指定時は
                ainvoke結果をフィルタリングし、reducer付きキーのみ
                返すことで並列fan-out時のInvalidUpdateErrorを防止する。
                Noneの場合は全キーを返す（直列ノード用）。
        """

        async def wrapped(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            self._update_stage(state, stage_name)
            result: dict[str, Any] = await subgraph.ainvoke(cast(Any, state), config=config)
            if output_keys is not None:
                return {k: v for k, v in result.items() if k in output_keys}
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

        # 並列ノードはreducer付きキーのみ返却し、InvalidUpdateErrorを防止
        # messages: MessagesStateの組込みreducer
        # metrics_results / logs_results: Annotated[..., _merge_list]
        if compiled_metrics is not None:
            graph.add_node(
                "investigate_metrics",
                self._wrap_with_stage(
                    compiled_metrics,
                    "メトリクスを調査中",
                    output_keys=frozenset({"messages", "metrics_results"}),
                ),
            )
            graph.add_edge("resolve_time_range", "investigate_metrics")
            graph.add_edge("investigate_metrics", "evaluate_results")
        else:
            logger.warning("MetricsAgent unavailable, skipping metrics investigation")

        if compiled_logs is not None:
            graph.add_node(
                "investigate_logs",
                self._wrap_with_stage(
                    compiled_logs,
                    "ログを調査中",
                    output_keys=frozenset({"messages", "logs_results"}),
                ),
            )
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
        graph.add_node(
            "generate_rca",
            self._wrap_with_stage(
                self._compiled_rca,
                "RCAレポートを生成中",
            ),
        )
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
                input_text = " ".join(
                    [
                        alert.alert_name or "",
                        alert.summary or "",
                        alert.description or "",
                    ]
                ).lower()

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
            detail = str(e)
            if isinstance(e, ExceptionGroup):
                from ai_agent_monitoring.tools.base import _flatten_exception_group

                leaves = _flatten_exception_group(e)
                detail = "; ".join(f"{type(exc).__name__}: {exc}" for exc in leaves)
            logger.warning(
                "Environment discovery failed: %s: %s",
                type(e).__name__,
                detail,
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
                promql = [q.query for q in env.discovered_panel_queries if q.query_type == "promql"]
                logql = [q.query for q in env.discovered_panel_queries if q.query_type == "logql"]
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

        query_text = self._get_query_text(state)
        user_query = state.get("user_query")

        if state["trigger_type"] == TriggerType.USER_QUERY and user_query is not None:
            sanitized_input = sanitize_user_input(user_query.raw_input)
            content = (
                f"ユーザからの問い合わせ:\n{sanitized_input}\n\n"
                "この問い合わせ内容を分析し、何を調査すべきか整理してください。"
            )
        else:
            alert = state.get("alert")
            if alert is not None:
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

    def _get_query_text(self, state: AgentState) -> str:
        """stateから調査対象のテキストを抽出（RAG検索用）."""
        if state.get("trigger_type") == TriggerType.USER_QUERY:
            user_query = state.get("user_query")
            if user_query:
                return user_query.raw_input
        alert = state.get("alert")
        if alert:
            return f"{alert.alert_name} {alert.summary} {alert.description}"
        return ""

    def _get_rag_query_examples(self, query_text: str) -> str:
        """RAGからクエリ生成に関連する具体的なコード例を取得."""
        if not query_text:
            return ""
        try:
            rag = get_query_rag()
            examples = rag.get_examples_for_task(query_text)
            if not examples:
                return ""
            return "\n".join(f"- `{ex}`" for ex in examples[:8])
        except Exception as e:
            logger.warning("Failed to get RAG query examples: %s", e)
            return ""

    def _format_panel_query_templates(self, env: EnvironmentContext | None) -> str:
        """環境の既存パネルクエリをテンプレート文字列にフォーマット."""
        if not env or not env.discovered_panel_queries:
            return ""
        promql_lines: list[str] = []
        logql_lines: list[str] = []
        for pq in env.discovered_panel_queries:
            label = f"- `{pq.query}`  ({pq.panel_title})" if pq.panel_title else f"- `{pq.query}`"
            if pq.query_type == "promql" and len(promql_lines) < 8:
                promql_lines.append(label)
            elif pq.query_type == "logql" and len(logql_lines) < 5:
                logql_lines.append(label)
        parts: list[str] = []
        if promql_lines:
            parts.append("PromQL:\n" + "\n".join(promql_lines))
        if logql_lines:
            parts.append("LogQL:\n" + "\n".join(logql_lines))
        return "\n".join(parts)

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
            lines.append("  - Prometheus: (grafana_list_datasources で取得してください)")
        if env.loki_datasource_uid:
            lines.append(f"  - Loki: `{env.loki_datasource_uid}`")
        else:
            lines.append("  - Loki: (grafana_list_datasources で取得してください)")

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

        RAGから関連するクエリ構文例を取得し、環境の既存ダッシュボード
        クエリをテンプレートとしてプロンプトに注入することで、
        LLMによるクエリ生成の精度を向上させる。
        """
        iteration = state.get("iteration_count", 0) + 1
        self._update_stage(state, f"調査計画を策定中 (イテレーション {iteration})")

        # RAG コンテキストの準備
        query_text = self._get_query_text(state)
        rag_examples = self._get_rag_query_examples(query_text)
        env = state.get("environment")
        panel_templates = self._format_panel_query_templates(env)

        # プロンプト構築
        plan_prompt_parts: list[str] = []

        # 前回の評価フィードバックがある場合
        feedback = state.get("evaluation_feedback")
        if feedback is not None:
            if feedback.missing_information:
                plan_prompt_parts.append(
                    "## 前回の調査で不足していた情報\n"
                    + "\n".join(f"- {item}" for item in feedback.missing_information)
                )

            if feedback.additional_investigation_points:
                plan_prompt_parts.append(
                    "## 追加で調査すべき観点\n"
                    + "\n".join(f"- {point}" for point in feedback.additional_investigation_points)
                )

            if feedback.previous_queries_attempted:
                plan_prompt_parts.append(
                    "## 前回試行済みのクエリ（同じクエリは避けてください）\n"
                    + "\n".join(f"- `{q}`" for q in feedback.previous_queries_attempted)
                )

            if feedback.reasoning:
                plan_prompt_parts.append(f"## 前回の評価理由\n{feedback.reasoning}")

        # RAG クエリ構文例
        if rag_examples:
            plan_prompt_parts.append(
                "## クエリ構文リファレンス\n"
                "以下は関連するクエリの構文例です。これらを参考にしてください:\n" + rag_examples
            )

        # 環境の既存パネルクエリをテンプレートとして提供
        if panel_templates:
            plan_prompt_parts.append(
                "## この環境で動作確認済みのクエリテンプレート\n"
                "以下はこの環境のダッシュボードで実際に使われているクエリです。\n"
                "ラベル名やメトリクス名を参考にしてください:\n" + panel_templates
            )

        # 最終指示
        if feedback is not None:
            plan_prompt_parts.append(
                "上記のフィードバックとリファレンスを踏まえ、前回とは異なるアプローチで"
                "調査計画をJSON形式で出力してください。\n"
                "promql_queries, logql_queries, target_instances, time_range を含めてください。\n"
                "time_rangeは必ずISO 8601絶対時刻のstart/endで指定してください。"
            )
        else:
            plan_prompt_parts.append(
                "上記の分析に基づき、調査計画をJSON形式で出力してください。\n"
                "promql_queries, logql_queries, target_instances, time_range を含めてください。\n"
                "time_rangeは必ずISO 8601絶対時刻のstart/endで指定してください。"
            )

        plan_prompt = "\n\n".join(plan_prompt_parts)

        messages = [
            *state["messages"],
            HumanMessage(content=plan_prompt),
        ]
        response = await self.llm.ainvoke(messages)

        plan = self._parse_plan(response.content)

        # 環境コンテキストからdatasource UIDを自動設定
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

    # クエリ修正リトライの最大回数
    _MAX_VALIDATION_RETRIES = 3

    @_observe(name="validate_queries", as_type="span")
    async def _validate_queries(self, state: AgentState) -> dict[str, Any]:
        """生成されたクエリを検証し、成功するまでLLMに修正を繰り返す.

        LLMが生成したPromQL/LogQLクエリの文法を検証し、
        エラーがある場合はLLMに修正を依頼する。最大 _MAX_VALIDATION_RETRIES 回
        繰り返し、有効なクエリが1つ以上揃うまで試行する。

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

        # datasource_uid の検証と自動修正
        self._fix_datasource_uids(plan, env)

        # バリデーション→修正ループ
        candidate_promql = list(plan.promql_queries)
        candidate_logql = list(plan.logql_queries)
        last_response = None

        for attempt in range(self._MAX_VALIDATION_RETRIES + 1):
            valid_promql, invalid_promql = self._validate_query_list(
                candidate_promql,
                QueryType.PROMQL,
            )
            valid_logql, invalid_logql = self._validate_query_list(
                candidate_logql,
                QueryType.LOGQL,
            )

            all_errors = invalid_promql + invalid_logql

            # 有効なクエリが1つ以上あればOK
            if valid_promql or valid_logql:
                plan.promql_queries = valid_promql
                plan.logql_queries = valid_logql
                if all_errors:
                    logger.info(
                        "Validation passed with %d valid queries (%d errors ignored)",
                        len(valid_promql) + len(valid_logql),
                        len(all_errors),
                    )
                result: dict[str, Any] = {"plan": plan}
                if last_response:
                    result["messages"] = [last_response]
                return result

            # 全てエラーかつリトライ回数が残っている場合、LLMに修正を依頼
            if attempt >= self._MAX_VALIDATION_RETRIES:
                break

            logger.warning(
                "Query validation attempt %d/%d: all queries invalid, asking LLM to fix",
                attempt + 1,
                self._MAX_VALIDATION_RETRIES,
            )

            last_response, new_plan = await self._request_query_regeneration(
                state,
                all_errors,
                attempt,
            )
            candidate_promql = new_plan.promql_queries
            candidate_logql = new_plan.logql_queries

        # 最大リトライ到達 — 空のまま返す
        logger.error(
            "Query validation failed after %d retries, no valid queries",
            self._MAX_VALIDATION_RETRIES,
        )
        plan.promql_queries = []
        plan.logql_queries = []
        result = {"plan": plan}
        if last_response:
            result["messages"] = [last_response]
        return result

    def _fix_datasource_uids(
        self,
        plan: InvestigationPlan,
        env: EnvironmentContext | None,
    ) -> None:
        """datasource_uid を検証し、環境コンテキストで自動修正."""
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

    def _validate_query_list(
        self,
        queries: list[str],
        query_type: QueryType,
    ) -> tuple[list[str], list[str]]:
        """クエリリストを検証し、有効/無効に分類.

        Returns:
            tuple[list[str], list[str]]:
                (有効なクエリリスト, エラー説明リスト)
        """
        valid: list[str] = []
        errors: list[str] = []
        type_label = "PromQL" if query_type == QueryType.PROMQL else "LogQL"

        validate_fn = (
            self.query_validator.validate_promql
            if query_type == QueryType.PROMQL
            else self.query_validator.validate_logql
        )

        for query in queries:
            # サニタイズ（二重ブレース、Grafana変数）
            sanitized, warnings = self.query_validator.sanitize_query(query, query_type)
            for w in warnings:
                logger.warning("%s sanitize warning: %s - %s", type_label, query, w)

            # Grafana変数を含む場合はスキップ
            if self.query_validator.contains_grafana_variables(sanitized):
                logger.info("Skipping query with Grafana variables: %s", sanitized)
                continue

            result = validate_fn(sanitized)
            if result.is_valid:
                valid.append(sanitized)
            elif result.corrected_query:
                revalidated = validate_fn(result.corrected_query)
                if revalidated.is_valid:
                    valid.append(result.corrected_query)
                    logger.info("%s auto-corrected: %s -> %s", type_label, query, result.corrected_query)
                else:
                    errors.append(f"{type_label}: {query} - {', '.join(result.errors or [])}")
            else:
                errors.append(f"{type_label}: {query} - {', '.join(result.errors or [])}")

        return valid, errors

    async def _request_query_regeneration(
        self,
        state: AgentState,
        validation_errors: list[str],
        attempt: int,
    ) -> tuple[Any, InvestigationPlan]:
        """バリデーションエラーを伝えてLLMにクエリ再生成を依頼.

        Returns:
            tuple[AIMessage, InvestigationPlan]: LLMのレスポンスとパースされたプラン
        """
        # RAGから関連ドキュメントを取得
        error_keywords = " ".join(q.split(":")[0] for q in validation_errors)
        rag_context = self._get_rag_context(error_keywords, max_tokens=1000)

        fewshot = get_all_fewshot_examples()
        error_msg = "\n".join(validation_errors)

        retry_content = f"生成されたクエリに文法エラーがありました（修正試行 {attempt + 1}回目）:\n{error_msg}\n\n"
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

        return response, new_plan

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
            feedback = self._parse_evaluation_feedback(response.content, previous_queries)
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
            feedback.additional_investigation_points = data.get("additional_investigation_points", [])
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

    # InvestigationPlan のフィールド名（LLMが別名で出力した場合の変換用）
    _PLAN_FIELD_ALIASES: ClassVar[dict[str, str]] = {
        "promql": "promql_queries",
        "logql": "logql_queries",
        "prometheus_queries": "promql_queries",
        "loki_queries": "logql_queries",
        "instances": "target_instances",
        "targets": "target_instances",
    }

    def _parse_plan(self, content: str) -> InvestigationPlan:
        """LLM出力から調査計画をパース.

        小さなモデルが不正確なJSON構造を出力するケースに対応:
        - 余分なフィールド → InvestigationPlan(extra="ignore") で無視
        - ネストされた計画オブジェクト → 自動抽出
        - フィールド名の揺れ → エイリアス変換
        - クエリフィールドが文字列 → リストに変換

        Raises:
            ValueError: 調査計画のパースに失敗した場合
        """
        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)

            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data).__name__}")

            # ネストされた計画オブジェクトを探索
            data = self._unwrap_nested_plan(data)

            # フィールド名のエイリアス変換
            for alias, canonical in self._PLAN_FIELD_ALIASES.items():
                if alias in data and canonical not in data:
                    data[canonical] = data.pop(alias)

            # クエリフィールドが文字列の場合はリストに変換
            for key in ("promql_queries", "logql_queries", "target_instances"):
                val = data.get(key)
                if isinstance(val, str):
                    data[key] = [val] if val.strip() else []

            # time_rangeの正規化: LLMが様々な形式で出力する可能性に対応
            if "time_range" in data and data["time_range"] is not None:
                tr = data["time_range"]
                if isinstance(tr, str):
                    logger.debug("time_range is string, setting to None: %s", tr)
                    data["time_range"] = None
                elif isinstance(tr, dict):
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
    def _unwrap_nested_plan(data: dict[str, Any]) -> dict[str, Any]:
        """ネストされた計画オブジェクトを抽出.

        LLMが {"investigation_plan": {...}} のようにラップして出力した場合、
        内側のdictを返す。promql_queries/logql_queries を含むdictを優先する。
        """
        # トップレベルにクエリフィールドがあればそのまま返す
        plan_keys = {"promql_queries", "logql_queries", "promql", "logql"}
        if plan_keys & data.keys():
            return data

        # 値がdictであるフィールドを探し、クエリフィールドを含むものを返す
        for value in data.values():
            if isinstance(value, dict) and plan_keys & value.keys():
                return value

        return data

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
