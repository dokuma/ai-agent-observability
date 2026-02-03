"""Metrics Analysis Agent — Prometheus メトリクス分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import METRICS_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import MetricsResult
from ai_agent_monitoring.core.state import AgentState
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import create_grafana_tools
from ai_agent_monitoring.tools.prometheus import create_prometheus_tools

logger = logging.getLogger(__name__)


class MetricsAgent:
    """Metrics Analysis Agent.

    Orchestrator から委任された PromQL クエリを実行し、
    メトリクスデータの異常パターンを分析する。
    """

    def __init__(self, llm: Any, prometheus_mcp: MCPClient, grafana_mcp: MCPClient | None = None) -> None:
        self.tools = create_prometheus_tools(prometheus_mcp)
        if grafana_mcp:
            self.tools += create_grafana_tools(grafana_mcp)
        self.llm = llm.bind_tools(self.tools)
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph[AgentState]:
        graph = StateGraph(AgentState)

        graph.add_node("reason", self._reason)
        graph.add_node("tools", ToolNode(self.tools))
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

    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """ReActループ: 思考し、必要ならToolを呼び出す."""
        plan = state.get("plan")
        if not plan:
            return {"messages": [AIMessage(content="調査計画がありません。")]}

        # 初回のみシステムプロンプトと調査指示を付与
        if not any(
            isinstance(m, SystemMessage) and "Metrics Agent" in m.content
            for m in state.get("messages", [])
        ):
            time_desc = "指定なし"
            if plan.time_range:
                time_desc = (
                    f"{plan.time_range.start.isoformat()} 〜 "
                    f"{plan.time_range.end.isoformat()}"
                )

            queries_text = "\n".join(f"- {q}" for q in plan.promql_queries)
            setup_messages = [
                SystemMessage(content=METRICS_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"以下のPromQLクエリでメトリクスを調査してください:\n{queries_text}\n"
                        f"対象インスタンス: {', '.join(plan.target_instances) or '全て'}\n"
                        f"時間範囲: {time_desc}\n\n"
                        "Toolを使ってクエリを実行し、結果を分析してください。"
                    )
                ),
            ]
            messages: list[BaseMessage] = setup_messages
        else:
            messages = list(state["messages"])

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をサマリとしてMetricsResultに変換."""
        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "これまでのメトリクス調査結果をまとめてください。\n"
                    "- 実行したクエリ\n"
                    "- 検出した異常パターン\n"
                    "- 全体のサマリ"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        plan = state.get("plan")
        result = MetricsResult(
            query=", ".join(plan.promql_queries) if plan is not None else "",
            summary=response.content,
        )

        return {
            "messages": [response],
            "metrics_results": [result],
        }

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tool_call"
        return "done"
