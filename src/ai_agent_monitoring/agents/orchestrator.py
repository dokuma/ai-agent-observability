"""Orchestrator Agent — Multi-Agentワークフローの制御."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from ai_agent_monitoring.agents.logs_agent import LogsAgent
from ai_agent_monitoring.agents.metrics_agent import MetricsAgent
from ai_agent_monitoring.agents.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from ai_agent_monitoring.agents.rca_agent import RCAAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import TriggerType
from ai_agent_monitoring.core.state import AgentState, InvestigationPlan, TimeRange
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import create_grafana_tools

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """Orchestrator Agent.

    アラートまたはユーザクエリを受け取り、調査計画の策定、
    Metrics/Logs Agentへの委任、RCAレポート生成までを制御する。
    """

    def __init__(
        self,
        llm: Any,
        prometheus_mcp: MCPClient,
        loki_mcp: MCPClient,
        grafana_mcp: MCPClient,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or Settings()
        self.grafana_tools = create_grafana_tools(grafana_mcp)
        self.metrics_agent = MetricsAgent(llm, prometheus_mcp, grafana_mcp)
        self.logs_agent = LogsAgent(llm, loki_mcp, grafana_mcp)
        self.rca_agent = RCAAgent(llm, grafana_mcp=grafana_mcp)
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph[AgentState]:
        """LangGraphワークフローを構築."""
        graph = StateGraph(AgentState)

        # ノード登録
        graph.add_node("analyze_input", self._analyze_input)
        graph.add_node("plan_investigation", self._plan_investigation)
        graph.add_node("resolve_time_range", self._resolve_time_range_node)
        graph.add_node("investigate_metrics", self.metrics_agent.compile())
        graph.add_node("investigate_logs", self.logs_agent.compile())
        graph.add_node("evaluate_results", self._evaluate_results)
        graph.add_node("generate_rca", self.rca_agent.compile())

        # エッジ定義
        graph.set_entry_point("analyze_input")
        graph.add_edge("analyze_input", "plan_investigation")
        graph.add_edge("plan_investigation", "resolve_time_range")
        graph.add_edge("resolve_time_range", "investigate_metrics")
        graph.add_edge("resolve_time_range", "investigate_logs")
        graph.add_edge("investigate_metrics", "evaluate_results")
        graph.add_edge("investigate_logs", "evaluate_results")
        graph.add_conditional_edges(
            "evaluate_results",
            self._should_continue,
            {
                "continue": "plan_investigation",
                "finish": "generate_rca",
            },
        )
        graph.add_edge("generate_rca", END)

        return graph

    def compile(self) -> Any:
        """グラフをコンパイルして実行可能にする."""
        return self.graph.compile()

    # ---- ノード関数 ----

    async def _analyze_input(self, state: AgentState) -> dict[str, Any]:
        """入力（アラートまたはユーザクエリ）を分析."""
        user_query = state.get("user_query")
        if state["trigger_type"] == TriggerType.USER_QUERY and user_query is not None:
            content = (
                f"ユーザからの問い合わせ:\n{user_query.raw_input}\n\n"
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

        system_prompt = ORCHESTRATOR_SYSTEM_PROMPT.format(
            max_iterations=state.get("max_iterations", 3),
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]
        response = await self.llm.ainvoke(messages)

        return {"messages": [response]}

    async def _plan_investigation(self, state: AgentState) -> dict[str, Any]:
        """調査計画を策定."""
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

        return {
            "messages": [response],
            "plan": plan,
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    async def _resolve_time_range_node(self, state: AgentState) -> dict[str, Any]:
        """時間範囲を確定させるノード.

        LLMが調査計画でtime_rangeを出力できた場合はそのまま通過。
        できなかった場合:
        - Alert起動: アラート時刻から自動推定
        - ユーザクエリ起動: UserQueryの解析済み時間があればそれを使用、
          なければinterruptでユーザに問い合わせる
        """
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
        """LLM出力から調査計画をパース."""
        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            return InvestigationPlan(**data)
        except (json.JSONDecodeError, ValueError):
            logger.warning("調査計画のパースに失敗。デフォルト計画を使用。")
            return InvestigationPlan()

    @staticmethod
    def _format_time_range(time_range: TimeRange | None) -> str:
        """TimeRangeを人間可読な文字列に変換."""
        if time_range is None:
            return "指定なし"
        return f"{time_range.start.isoformat()} 〜 {time_range.end.isoformat()}"

    @staticmethod
    def _extract_json(text: str) -> str:
        """テキストからJSON部分を抽出."""
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()
        start = text.index("{")
        end = text.rindex("}") + 1
        return text[start:end]
