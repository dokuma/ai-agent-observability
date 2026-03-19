"""Kubernetes Analysis Agent — K8sクラスタ状態分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import KUBERNETES_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import KubernetesResult, ToolObservation
from ai_agent_monitoring.core.state import (
    AgentState,
    _extract_text_from_content,
    extract_tool_observations,
    extract_tool_outputs,
    should_stop_tool_loop,
)
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.context_store import ContextStore, extract_promql_search_terms
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

_MAX_REACT_STEPS = 5


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
        context_store: ContextStore | None = None,
    ) -> None:
        self.tools: list[Any] = []
        self._k8s_tool: KubernetesMCPTool | None = None
        self._context_store = context_store

        if kubernetes_mcp:
            # session_context() でセッション再利用するため、
            # @tool クロージャと同一インスタンスを共有する
            self._k8s_tool = KubernetesMCPTool(kubernetes_mcp, context_store=context_store)
            self.tools = create_kubernetes_tools(kubernetes_mcp, k8s_tool=self._k8s_tool, context_store=context_store)
            logger.info("KubernetesAgent: Using Kubernetes MCP")

        if not self.tools:
            logger.warning("KubernetesAgent: No MCP tools available!")

        self.llm = llm.bind_tools(self.tools) if self.tools else llm
        self._tool_node = ToolNode(self.tools, handle_tool_errors=True)
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
                result: dict[str, Any] = await self._tool_node.ainvoke(state)
                return result
        result = await self._tool_node.ainvoke(state)
        return result

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

            if investigation_details:
                details_text = "\n".join(investigation_details)
                investigation_scope = "Toolを使ってクラスタの状態を確認し、異常を分析してください。"
            else:
                details_text = "クラスタ全体の健康状態を調査"
                investigation_scope = (
                    "パターンBの手順に従ってください: "
                    "まず k8s_list_namespaces でnamespace一覧を取得し、"
                    "各namespaceごとに k8s_list_events でイベントを確認してください。"
                    "全namespaceを一括で取得するAPIコールは使わないでください。"
                )

            # 環境情報から取得済みの K8s サマリを含める
            k8s_env_text = self._format_k8s_env_summary(state)

            setup_messages: list[BaseMessage] = [
                SystemMessage(content=KUBERNETES_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"Kubernetesクラスタの状態を調査してください:\n{details_text}\n"
                        f"時間範囲: {time_desc}\n"
                        f"{k8s_env_text}"
                        f"{investigation_scope}"
                    )
                ),
            ]
            response = await self.llm.ainvoke(setup_messages)
            return {"messages": [*setup_messages, response]}
        else:
            messages = self._build_context_aware_messages(state, "Kubernetes Agent")

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    def _build_context_aware_messages(
        self,
        state: AgentState,
        agent_identifier: str,
    ) -> list[BaseMessage]:
        """Orchestratorメッセージを除外し、古いToolMessageを圧縮したメッセージリストを構築."""
        all_messages = list(state["messages"])

        agent_start_idx = 0
        for i, m in enumerate(all_messages):
            if isinstance(m, SystemMessage) and agent_identifier in m.content:
                agent_start_idx = i
                break
        agent_messages: list[BaseMessage] = list(all_messages[agent_start_idx:])

        if not self._context_store or len(agent_messages) <= 3:
            return agent_messages

        plan = state.get("plan")
        search_query = self._build_search_query(plan, agent_messages)
        return self._compress_old_messages(agent_messages, search_query)

    def _build_search_query(
        self,
        plan: Any,
        messages: list[BaseMessage],
    ) -> str:
        """調査計画と最新のAIMessageからContextStore検索クエリを構築.

        K8sエージェント用: namespace名、Pod名、リソース種別、
        インスタンス名を検索語として使用する。
        これらはツール出力に含まれる識別子と一致するため、
        FTS5 BM25検索で有効に機能する。
        """
        terms: list[str] = []
        if plan:
            if hasattr(plan, "target_namespaces") and plan.target_namespaces:
                terms.extend(plan.target_namespaces)
            if hasattr(plan, "target_pods") and plan.target_pods:
                terms.extend(plan.target_pods)
            if hasattr(plan, "k8s_resource_kinds") and plan.k8s_resource_kinds:
                terms.extend(plan.k8s_resource_kinds)
            if hasattr(plan, "target_instances") and plan.target_instances:
                terms.extend(plan.target_instances)

        # 最新のAIMessageから技術用語を抽出
        for m in reversed(messages):
            if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content:
                # PromQLが含まれる場合はメトリクス名・ラベル値を抽出
                terms.extend(extract_promql_search_terms(m.content))
                break

        return " ".join(terms) if terms else "kubernetes pod namespace event"

    def _compress_old_messages(
        self,
        messages: list[BaseMessage],
        search_query: str,
    ) -> list[BaseMessage]:
        """古いToolMessageの内容をContextStore検索結果で置換して圧縮."""
        if not self._context_store:
            return messages

        last_tool_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], ToolMessage):
                last_tool_idx = i
                break

        last_ai_idx = -1
        for i in range(last_tool_idx, -1, -1):
            if isinstance(messages[i], AIMessage):
                last_ai_idx = i
                break

        protect_from = last_ai_idx if last_ai_idx >= 0 else len(messages)

        compressed: list[BaseMessage] = []
        for i, msg in enumerate(messages):
            if i >= protect_from:
                compressed.append(msg)
            elif isinstance(msg, ToolMessage):
                search_results = self._context_store.search(search_query, limit=3)
                if search_results:
                    summary = "\n".join(c["content"] for c in search_results)
                    compressed.append(
                        ToolMessage(
                            content=f"[過去の調査結果から関連情報]\n{summary}",
                            tool_call_id=msg.tool_call_id,
                            name=getattr(msg, "name", None),
                        )
                    )
                else:
                    original = _extract_text_from_content(msg.content)
                    compressed.append(
                        ToolMessage(
                            content=original[:500] + "..." if len(original) > 500 else original,
                            tool_call_id=msg.tool_call_id,
                            name=getattr(msg, "name", None),
                        )
                    )
            else:
                compressed.append(msg)

        return compressed

    @_observe(name="kubernetes_agent_summarize", as_type="span")
    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をペアごとの観察に基づくKubernetesResultに変換."""
        raw_observations = extract_tool_observations(state["messages"])
        observations: list[ToolObservation] = []

        if raw_observations:
            observations = await self._build_observations(raw_observations)
            summary = await self._build_grounded_summary(observations)
        else:
            logger.info("KubernetesAgent: tool observations not found, using message-based summary")
            summary = await self._build_message_based_summary(state)

        result = KubernetesResult(
            summary=summary,
            observations=observations,
            tool_outputs=extract_tool_outputs(state["messages"]),
        )

        return {
            "messages": [HumanMessage(content=summary)],
            "k8s_results": [result],
        }

    async def _build_message_based_summary(self, state: AgentState) -> str:
        """フォールバック: メッセージ履歴全体からsummaryを生成（従来方式）."""
        from ai_agent_monitoring.core.state import sanitize_tool_call_messages

        messages = [
            *sanitize_tool_call_messages(state["messages"]),
            HumanMessage(
                content=(
                    "これまでのKubernetesクラスタ調査結果をまとめてください。\n"
                    "- 確認したリソースと状態\n"
                    "- 検出した異常\n"
                    "- 重要なイベント\n"
                    "- 全体のサマリ\n\n"
                    "**重要**: ツール実行結果に含まれない情報は記述しないでください。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)
        return _extract_text_from_content(response.content)

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
                "Pod名、ステータス、イベント種別は出力に記載されたものをそのまま引用してください。"
            )
            response = await self.llm.ainvoke([HumanMessage(content=fact_prompt)])
            observations.append(
                ToolObservation(
                    tool_name=obs["tool_name"],
                    tool_input=obs["tool_input"],
                    tool_output=obs["tool_output"],
                    observation=_extract_text_from_content(response.content),
                )
            )
        return observations

    async def _build_grounded_summary(self, observations: list[ToolObservation]) -> str:
        """観察結果を統合してsummaryを生成."""
        if not observations:
            return "ツール実行結果がありません。"

        obs_text = "\n\n".join(f"### {o.tool_name}({o.tool_input})\n{o.observation}" for o in observations)
        summary_prompt = (
            "以下の観察結果を統合してKubernetesクラスタ分析の要約を作成してください。\n"
            "観察結果に含まれない情報は記述しないでください。\n\n" + obs_text
        )
        response = await self.llm.ainvoke([HumanMessage(content=summary_prompt)])
        return _extract_text_from_content(response.content)

    @staticmethod
    def _format_k8s_env_summary(state: AgentState) -> str:
        """環境情報から取得済みのK8sクラスタサマリをフォーマット."""
        env = state.get("environment")
        if not env or not env.k8s_env.namespaces:
            return ""

        k8s = env.k8s_env
        lines = ["\n## 取得済みクラスタ情報（再取得不要）"]
        if k8s.node_count:
            lines.append(f"ノード数: {k8s.node_count}")
        lines.append(f"Namespace一覧: {', '.join(k8s.namespaces)}")
        for ns, summary in k8s.namespace_summaries.items():
            status_parts = [f"{s}: {c}" for s, c in summary.pod_statuses.items()]
            status_str = ", ".join(status_parts) if status_parts else "不明"
            line = f"- {ns}: Pod {summary.pod_count}個 ({status_str})"
            if summary.warning_event_count:
                line += f", Warning events: {summary.warning_event_count}"
            lines.append(line)

        lines.append(
            "\n上記の情報はすでに取得済みです。"
            "k8s_list_namespaces や概要レベルの k8s_list_pods は不要です。"
            "異常が疑われるPodの詳細ログや特定リソースの調査に集中してください。\n"
        )
        return "\n".join(lines)

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        result = should_stop_tool_loop(state["messages"], _MAX_REACT_STEPS)
        if result == "done":
            logger.info("KubernetesAgent: tool loop ended")
        return result or "done"
