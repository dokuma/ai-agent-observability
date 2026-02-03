"""Logs Analysis Agent — Loki ログ分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import LOGS_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import LogsResult
from ai_agent_monitoring.core.state import AgentState
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import create_grafana_tools
from ai_agent_monitoring.tools.loki import create_loki_tools

logger = logging.getLogger(__name__)


class LogsAgent:
    """Logs Analysis Agent.

    Orchestrator から委任された LogQL クエリを実行し、
    エラーパターンやログの異常を分析する。

    Grafana MCP が利用可能な場合は優先的に使用し、
    Loki MCP はフォールバックとして使用する。
    """

    def __init__(
        self,
        llm: Any,
        loki_mcp: MCPClient | None = None,
        grafana_mcp: MCPClient | None = None,
    ) -> None:
        self.tools: list[Any] = []

        # Grafana MCPを優先（Grafana経由でLokiにアクセス可能）
        if grafana_mcp:
            self.tools += create_grafana_tools(grafana_mcp)
            logger.info("LogsAgent: Using Grafana MCP (primary)")

        # Loki MCPはフォールバック
        if loki_mcp:
            self.tools += create_loki_tools(loki_mcp)
            logger.info("LogsAgent: Using Loki MCP (fallback)")

        if not self.tools:
            logger.warning("LogsAgent: No MCP tools available!")

        self.llm = llm.bind_tools(self.tools) if self.tools else llm
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
            isinstance(m, SystemMessage) and "Logs Agent" in m.content
            for m in state.get("messages", [])
        ):
            time_desc = "指定なし"
            if plan.time_range:
                time_desc = (
                    f"{plan.time_range.start.isoformat()} 〜 "
                    f"{plan.time_range.end.isoformat()}"
                )

            queries_text = "\n".join(f"- {q}" for q in plan.logql_queries)
            setup_messages = [
                SystemMessage(content=LOGS_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"以下のLogQLクエリでログを調査してください:\n{queries_text}\n"
                        f"対象インスタンス: {', '.join(plan.target_instances) or '全て'}\n"
                        f"時間範囲: {time_desc}\n\n"
                        "Toolを使ってクエリを実行し、エラーパターンを分析してください。"
                    )
                ),
            ]
            messages: list[BaseMessage] = setup_messages
        else:
            messages = list(state["messages"])

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をサマリとしてLogsResultに変換."""
        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "これまでのログ調査結果をまとめてください。\n"
                    "- 実行したクエリ\n"
                    "- 検出したエラーパターン\n"
                    "- 全体のサマリ"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        plan = state.get("plan")
        result = LogsResult(
            query=", ".join(plan.logql_queries) if plan is not None else "",
            summary=response.content,
        )

        return {
            "messages": [response],
            "logs_results": [result],
        }

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tool_call"
        return "done"
