"""Logs Analysis Agent — Loki ログ分析."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from ai_agent_monitoring.agents.prompts import LOGS_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import LogsResult, ToolObservation
from ai_agent_monitoring.core.state import (
    AgentState,
    _extract_text_from_content,
    extract_tool_observations,
    extract_tool_outputs,
    should_stop_tool_loop,
)
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.context_store import ContextStore
from ai_agent_monitoring.tools.grafana import create_grafana_tools
from ai_agent_monitoring.tools.loki import create_loki_tools

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
        context_store: ContextStore | None = None,
    ) -> None:
        self.tools: list[Any] = []
        self._context_store = context_store

        # Grafana MCPを優先（Grafana経由でLokiにアクセス可能）
        if grafana_mcp:
            self.tools += create_grafana_tools(grafana_mcp, context_store=context_store)
            logger.info("LogsAgent: Using Grafana MCP (primary)")

        # Loki MCPはフォールバック
        if loki_mcp:
            self.tools += create_loki_tools(loki_mcp, context_store=context_store)
            logger.info("LogsAgent: Using Loki MCP (fallback)")

        if not self.tools:
            logger.warning("LogsAgent: No MCP tools available!")

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

    @_observe(name="logs_agent_reason", as_type="span")
    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """ReActループ: 思考し、必要ならToolを呼び出す."""
        plan = state.get("plan")
        if not plan:
            return {"messages": [AIMessage(content="調査計画がありません。")]}

        # 初回のみシステムプロンプトと調査指示を付与
        if not any(isinstance(m, SystemMessage) and "Logs Agent" in m.content for m in state.get("messages", [])):
            time_desc = "指定なし"
            if plan.time_range:
                time_desc = f"{plan.time_range.start.isoformat()} 〜 {plan.time_range.end.isoformat()}"

            queries_text = "\n".join(f"- {q}" for q in plan.logql_queries)
            datasource_uids = plan.loki_datasource_uids

            # datasource_uids の有効性でプロンプトを分岐
            valid_uids = [uid for uid in datasource_uids if uid and not uid.startswith("(")]
            if len(valid_uids) == 1:
                datasource_instruction = (
                    f"LokiデータソースUID: `{valid_uids[0]}`\n\n"
                    "**重要**: grafana_query_lokiを使用する際は、"
                    f"必ず `datasource_uid='{valid_uids[0]}'` を指定してください。"
                )
            elif len(valid_uids) > 1:
                # 複数DS: 環境情報からDS別のラベル例を取得
                env = state.get("environment")
                ds_descriptions: list[str] = []
                for uid in valid_uids:
                    ds_name = uid
                    if env:
                        for ds in env.loki_datasources:
                            if ds.uid == uid:
                                ds_name = f"{ds.name} (uid: `{uid}`)"
                                break
                        loki_info = env.loki_env_by_uid.get(uid)
                        if loki_info and loki_info.jobs:
                            examples = ", ".join(loki_info.jobs[:5])
                            ds_descriptions.append(f"- {ds_name}: job例: [{examples}]")
                        else:
                            ds_descriptions.append(f"- {ds_name}")
                    else:
                        ds_descriptions.append(f"- uid: `{uid}`")
                ds_list_text = "\n".join(ds_descriptions)
                datasource_instruction = (
                    f"利用可能なLokiデータソース:\n{ds_list_text}\n\n"
                    "**重要**: grafana_query_lokiを使用する際は、"
                    "クエリ内容に応じて適切な datasource_uid を指定してください。"
                )
            else:
                datasource_instruction = (
                    "**注意**: LokiデータソースUIDが設定されていません。\n"
                    "最初に grafana_list_datasources を呼び出してLokiデータソースの"
                    "uidを取得し、そのuidを grafana_query_loki に指定してください。"
                )

            setup_messages: list[BaseMessage] = [
                SystemMessage(content=LOGS_AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"以下のLogQLクエリでログを調査してください:\n{queries_text}\n"
                        f"対象インスタンス: {', '.join(plan.target_instances) or '全て'}\n"
                        f"時間範囲: {time_desc}\n"
                        f"{datasource_instruction}\n"
                        '**重要**: LogQLは {job="xxx"} |= "error" の形式です。'
                        "SQLではありません。二重ブレース {{...}} は使用しないでください。\n"
                        "Toolを使ってクエリを実行し、エラーパターンを分析してください。"
                    )
                ),
            ]
            response = await self.llm.ainvoke(setup_messages)
            return {"messages": [*setup_messages, response]}
        else:
            messages = self._build_context_aware_messages(state, "Logs Agent")

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
        """調査計画と最新のAIMessageからContextStore検索クエリを構築."""
        parts: list[str] = []
        if plan:
            if hasattr(plan, "logql_queries") and plan.logql_queries:
                parts.extend(plan.logql_queries[:3])
            if hasattr(plan, "promql_queries") and plan.promql_queries:
                parts.extend(plan.promql_queries[:3])
            if hasattr(plan, "target_instances") and plan.target_instances:
                parts.extend(plan.target_instances[:3])

        for m in reversed(messages):
            if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content:
                parts.append(m.content[:200])
                break

        return " ".join(parts) if parts else "logs loki query error"

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

    @_observe(name="logs_agent_summarize", as_type="span")
    async def _summarize(self, state: AgentState) -> dict[str, Any]:
        """Tool実行結果をペアごとの観察に基づくLogsResultに変換."""
        raw_observations = extract_tool_observations(state["messages"])
        observations: list[ToolObservation] = []

        if raw_observations:
            observations = await self._build_observations(raw_observations)
            summary = await self._build_grounded_summary(observations)
        else:
            logger.info("LogsAgent: tool observations not found, using message-based summary")
            summary = await self._build_message_based_summary(state)

        plan = state.get("plan")
        result = LogsResult(
            query=", ".join(plan.logql_queries) if plan is not None else "",
            summary=summary,
            observations=observations,
            tool_outputs=extract_tool_outputs(state["messages"]),
        )

        return {
            "messages": [HumanMessage(content=summary)],
            "logs_results": [result],
        }

    async def _build_message_based_summary(self, state: AgentState) -> str:
        """フォールバック: メッセージ履歴全体からsummaryを生成（従来方式）."""
        from ai_agent_monitoring.core.state import sanitize_tool_call_messages

        messages = [
            *sanitize_tool_call_messages(state["messages"]),
            HumanMessage(
                content=(
                    "これまでのログ調査結果をまとめてください。\n"
                    "- 実行したクエリとその結果\n"
                    "- 検出したエラーパターン\n"
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
                "数値やステータスは出力に記載されたものをそのまま引用してください。"
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
            "以下の観察結果を統合してログ分析の要約を作成してください。\n"
            "観察結果に含まれない情報は記述しないでください。\n\n" + obs_text
        )
        response = await self.llm.ainvoke([HumanMessage(content=summary_prompt)])
        return _extract_text_from_content(response.content)

    @staticmethod
    def _should_use_tool(state: AgentState) -> str:
        """最後のメッセージにtool_callがあればToolを実行."""
        result = should_stop_tool_loop(state["messages"], _MAX_REACT_STEPS)
        if result == "done":
            logger.info("LogsAgent: tool loop ended")
        return result or "done"
