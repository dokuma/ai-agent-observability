"""Kubernetes Analysis Agent — K8sクラスタ状態分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import KUBERNETES_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import KubernetesResult
from ai_agent_monitoring.core.state import AgentState
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.kubernetes import KubernetesMCPTool, create_kubernetes_tools

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


class KubernetesAgent:
    """Kubernetes Analysis Agent.

    Orchestrator から委任された K8s クラスタ調査を実行し、
    Pod状態・イベント・リソース使用状況の異常を分析する。

    SSE 接続チャーンを防ぐため、ToolNode 実行時に
    KubernetesMCPTool のセッションを再利用する。
    """

    def __init__(
        self,
        llm: Any,
        kubernetes_mcp: MCPClient | None = None,
    ) -> None:
        self.tools: list[Any] = []
        self._k8s_tool: KubernetesMCPTool | None = None

        if kubernetes_mcp:
            self._k8s_tool = KubernetesMCPTool(kubernetes_mcp)
            self.tools = create_kubernetes_tools(kubernetes_mcp)
            logger.info("KubernetesAgent: Using Kubernetes MCP")

        if not self.tools:
            logger.warning("KubernetesAgent: No MCP tools available!")

        self.llm = llm.bind_tools(self.tools) if self.tools else llm
        self._tool_node = ToolNode(self.tools)
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph[AgentState]:
        graph = StateGraph(AgentState)

        graph.add_node("reason", self._reason)
        graph.add_node("tools", self._tools_with_session)
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

    async def _tools_with_session(self, state: AgentState) -> dict[str, Any]:
        """セッションを再利用して ToolNode を実行.

        各ツール呼び出しで新しい SSE 接続を作成する代わりに、
        KubernetesMCPTool のセッションコンテキスト内で ToolNode を実行し、
        同一の MCP セッションを再利用する。これにより Go サーバーへの
        急速な接続/切断チャーンを防ぐ。
        """
        if self._k8s_tool:
            async with self._k8s_tool.session_context():
                return await self._tool_node.ainvoke(state)
        return await self._tool_node.ainvoke(state)

    @_observe(name="kubernetes_agent_reason", as_type="span")
    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """ReActループ: 思考し、必要ならToolを呼び出す."""
        plan = state.get("plan")
        if not plan:
            return {"messages": [AIMessage(content="調査計画がありません。")]}

        # 初回のみシステムプロンプトと調査指示を付与
        if not any(isinstance(m, SystemMessage) and "Kubernetes Agent" in m.content for m in state.get("messages", [])):
            time_desc = "指定なし"
            if plan.time_range:
                time_desc = f"{plan.time_range.start.isoformat()} 〜 {plan.time_range.end.isoformat()}"

            # InvestigationPlanのK8sフィールドを安全に参照
            target_namespaces = getattr(plan, "target_namespaces", [])
            target_pods = getattr(plan, "target_pods", [])
            k8s_resource_kinds = getattr(plan, "k8s_resource_kinds", [])

            investigation_details = []
            if target_namespaces:
                investigation_details.append(f"対象Namespace: {', '.join(target_namespaces)}")
            if target_pods:
                investigation_details.append(f"対象Pod: {', '.join(target_pods)}")
            if k8s_resource_kinds:
                investigation_details.append(f"確認リソース種別: {', '.join(k8s_resource_kinds)}")
            if plan.target_instances:
                investigation_details.append(f"対象インスタンス: {', '.join(plan.target_instances)}")

            details_text = "\n".join(investigation_details) if investigation_details else "全般的な調査"

            setup_messages: list[BaseMessage] = [
                SystemMessage(content=KUBERNETES_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"Kubernetesクラスタの状態を調査してください:\n{details_text}\n"
                        f"時間範囲: {time_desc}\n"
                        "Toolを使ってクラスタの状態を確認し、異常を分析してください。"
                    )
                ),
            ]
            response = await self.llm.ainvoke(setup_messages)
            return {"messages": [*setup_messages, response]}
        else:
            messages = list(state["messages"])

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    @_observe(name="kubernetes_agent_summarize", as_type="span")
    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をサマリとしてKubernetesResultに変換."""
        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "これまでのKubernetesクラスタ調査結果をまとめてください。\n"
                    "- 確認したリソースと状態\n"
                    "- 検出した異常（CrashLoopBackOff, OOMKilled, Pending等）\n"
                    "- 重要なイベント\n"
                    "- 全体のサマリ"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        result = KubernetesResult(
            summary=response.content,
        )

        return {
            "messages": [response],
            "k8s_results": [result],
        }

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tool_call"
        return "done"
