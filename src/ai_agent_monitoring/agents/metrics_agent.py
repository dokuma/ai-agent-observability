"""Metrics Analysis Agent — Prometheus メトリクス分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import METRICS_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import MetricsResult, ToolObservation
from ai_agent_monitoring.core.state import (
    AgentState,
    extract_tool_observations,
    extract_tool_outputs,
    should_stop_tool_loop,
)
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.context_store import ContextStore
from ai_agent_monitoring.tools.grafana import create_grafana_tools
from ai_agent_monitoring.tools.prometheus import create_prometheus_tools

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe
except ImportError:

    def _observe(func: Any = None, **kwargs: Any) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func
        return lambda f: f


logger = logging.getLogger(__name__)

_MAX_REACT_STEPS = 5


class MetricsAgent:
    """Metrics Analysis Agent.

    Orchestrator から委任された PromQL クエリを実行し、
    メトリクスデータの異常パターンを分析する。

    Grafana MCP が利用可能な場合は優先的に使用し、
    Prometheus MCP はフォールバックとして使用する。
    """

    def __init__(
        self,
        llm: Any,
        prometheus_mcp: MCPClient | None = None,
        grafana_mcp: MCPClient | None = None,
        context_store: ContextStore | None = None,
    ) -> None:
        self.tools: list[Any] = []

        # Grafana MCPを優先（Grafana経由でPrometheusにアクセス可能）
        if grafana_mcp:
            self.tools += create_grafana_tools(grafana_mcp, context_store=context_store)
            logger.info("MetricsAgent: Using Grafana MCP (primary)")

        # Prometheus MCPはフォールバック
        if prometheus_mcp:
            self.tools += create_prometheus_tools(prometheus_mcp, context_store=context_store)
            logger.info("MetricsAgent: Using Prometheus MCP (fallback)")

        if not self.tools:
            logger.warning("MetricsAgent: No MCP tools available!")

        self.llm = llm.bind_tools(self.tools) if self.tools else llm
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph[AgentState]:
        graph = StateGraph(AgentState)

        graph.add_node("reason", self._reason)
        graph.add_node("tools", ToolNode(self.tools, handle_tool_errors=True))
        graph.add_node("summarize", self._summarize)

        graph.set_entry_point("reason")
        graph.add_conditional_edges(
            "reason",
            self._should_use_tool,
            {"tool_call": "tools", "done": "summarize"},
        )
        graph.add_edge("tools", "reason")
        graph.add_edge("summarize", END)

        return graph

    def compile(self) -> Any:
        """グラフをコンパイル."""
        return self.graph.compile()

    @_observe(name="metrics_agent_reason", as_type="span")
    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """ReActループ: 思考し、必要ならToolを呼び出す."""
        plan = state.get("plan")
        if not plan:
            return {"messages": [AIMessage(content="調査計画がありません。")]}

        # 初回のみシステムプロンプトと調査指示を付与
        if not any(isinstance(m, SystemMessage) and "Metrics Agent" in m.content for m in state.get("messages", [])):
            time_desc = "指定なし"
            if plan.time_range:
                time_desc = f"{plan.time_range.start.isoformat()} 〜 {plan.time_range.end.isoformat()}"

            queries_text = "\n".join(f"- {q}" for q in plan.promql_queries)
            datasource_uids = plan.prometheus_datasource_uids

            # datasource_uids の有効性でプロンプトを分岐
            valid_uids = [uid for uid in datasource_uids if uid and not uid.startswith("(")]
            if len(valid_uids) == 1:
                datasource_instruction = (
                    f"Prometheusデータソースuid: `{valid_uids[0]}`\n\n"
                    "**重要**: grafana_query_prometheusを使用する際は、"
                    f"必ず `datasource_uid='{valid_uids[0]}'` を指定してください。"
                )
            elif len(valid_uids) > 1:
                # 複数DS: 環境情報からDS別のメトリクス例を取得
                env = state.get("environment")
                ds_descriptions: list[str] = []
                for uid in valid_uids:
                    ds_name = uid
                    if env:
                        for ds in env.prometheus_datasources:
                            if ds.uid == uid:
                                ds_name = f"{ds.name} (uid: `{uid}`)"
                                break
                        prom_info = env.prometheus_env_by_uid.get(uid)
                        if prom_info and prom_info.metrics:
                            examples = ", ".join(prom_info.metrics[:5])
                            ds_descriptions.append(f"- {ds_name}: メトリクス例: [{examples}]")
                        else:
                            ds_descriptions.append(f"- {ds_name}")
                    else:
                        ds_descriptions.append(f"- uid: `{uid}`")
                ds_list_text = "\n".join(ds_descriptions)
                datasource_instruction = (
                    f"利用可能なPrometheusデータソース:\n{ds_list_text}\n\n"
                    "**重要**: grafana_query_prometheusを使用する際は、"
                    "クエリ内容に応じて適切な datasource_uid を指定してください。"
                )
            else:
                datasource_instruction = (
                    "**注意**: Prometheusデータソースuidが設定されていません。\n"
                    "最初に grafana_list_datasources を呼び出してPrometheusデータソースの"
                    "uidを取得し、そのuidを grafana_query_prometheus に指定してください。"
                )

            setup_messages: list[BaseMessage] = [
                SystemMessage(content=METRICS_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"以下のPromQLクエリでメトリクスを調査してください:\n{queries_text}\n"
                        f"対象インスタンス: {', '.join(plan.target_instances) or '全て'}\n"
                        f"時間範囲: {time_desc}\n"
                        f"{datasource_instruction}\n"
                        "Toolを使ってクエリを実行し、結果を分析してください。"
                    )
                ),
            ]
            response = await self.llm.ainvoke(setup_messages)
            return {"messages": [*setup_messages, response]}
        else:
            messages = list(state["messages"])

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    @_observe(name="metrics_agent_summarize", as_type="span")
    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をペアごとの観察に基づくMetricsResultに変換."""
        # 1. ツール実行ペアを抽出
        raw_observations = extract_tool_observations(state["messages"])
        observations: list[ToolObservation] = []

        if raw_observations:
            observations = await self._build_observations(raw_observations)
            summary = await self._build_grounded_summary(observations)
        else:
            # フォールバック: tool_callsが見つからない場合は従来方式
            logger.info("MetricsAgent: tool observations not found, using message-based summary")
            summary = await self._build_message_based_summary(state)

        plan = state.get("plan")
        result = MetricsResult(
            query=", ".join(plan.promql_queries) if plan is not None else "",
            summary=summary,
            observations=observations,
            tool_outputs=extract_tool_outputs(state["messages"]),
        )

        return {
            "messages": [HumanMessage(content=summary)],
            "metrics_results": [result],
        }

    async def _build_message_based_summary(self, state: AgentState) -> str:
        """フォールバック: メッセージ履歴全体からsummaryを生成（従来方式）."""
        from ai_agent_monitoring.core.state import sanitize_tool_call_messages

        messages = [
            *sanitize_tool_call_messages(state["messages"]),
            HumanMessage(
                content=(
                    "これまでのメトリクス調査結果をまとめてください。\n"
                    "- 実行したクエリとその結果\n"
                    "- 検出した異常パターン\n"
                    "- 全体のサマリ\n\n"
                    "**重要**: ツール実行結果に含まれない情報は記述しないでください。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)
        return str(response.content)

    async def _build_observations(self, raw_observations: list[dict[str, str]]) -> list[ToolObservation]:
        """各ツール実行ペアからLLMで事実を抽出."""
        observations: list[ToolObservation] = []
        for obs in raw_observations:
            fact_prompt = (
                f"ツール: {obs['tool_name']}\n"
                f"入力: {obs['tool_input']}\n"
                f"出力:\n{obs['tool_output']}\n\n"
                "上記の出力から読み取れる事実のみを箇条書きで記述してください。\n"
                "出力に含まれない情報は絶対に記述しないでください。\n"
                "数値やステータスは出力に記載されたものをそのまま引用してください。"
            )
            response = await self.llm.ainvoke([HumanMessage(content=fact_prompt)])
            observations.append(
                ToolObservation(
                    tool_name=obs["tool_name"],
                    tool_input=obs["tool_input"],
                    tool_output=obs["tool_output"],
                    observation=response.content,
                )
            )
        return observations

    async def _build_grounded_summary(self, observations: list[ToolObservation]) -> str:
        """観察結果を統合してsummaryを生成."""
        if not observations:
            return "ツール実行結果がありません。"

        obs_text = "\n\n".join(f"### {o.tool_name}({o.tool_input})\n{o.observation}" for o in observations)
        summary_prompt = (
            "以下の観察結果を統合してメトリクス分析の要約を作成してください。\n"
            "観察結果に含まれない情報は記述しないでください。\n\n" + obs_text
        )
        response = await self.llm.ainvoke([HumanMessage(content=summary_prompt)])
        return str(response.content)

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        result = should_stop_tool_loop(state["messages"], _MAX_REACT_STEPS)
        if result == "done":
            logger.info("MetricsAgent: tool loop ended")
        return result or "done"
