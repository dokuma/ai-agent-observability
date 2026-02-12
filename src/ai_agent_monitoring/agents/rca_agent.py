"""RCA Agent — 根本原因分析."""

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from ai_agent_monitoring.agents.prompts import RCA_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.models import (
    LogExcerpt,
    PanelSnapshot,
    RCAReport,
    RootCause,
)
from ai_agent_monitoring.core.renderer import render_rca_markdown
from ai_agent_monitoring.core.state import AgentState
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import GrafanaMCPTool

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe
except ImportError:
    def _observe(
        func: Any = None, **kwargs: Any
    ) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func
        return lambda f: f

logger = logging.getLogger(__name__)


class RCAAgent:
    """Root Cause Analysis Agent.

    Metrics Agent と Logs Agent の分析結果を統合し、
    根本原因を推論してレポートを生成する。
    グラフ画像・ログ抜粋を含むMarkdownレポートを出力する。
    """

    def __init__(
        self,
        llm: Any,
        grafana_mcp: MCPClient | None = None,
        output_dir: str = "/tmp/rca_reports",  # noqa: S108
    ) -> None:
        self.llm = llm
        self.grafana = GrafanaMCPTool(grafana_mcp) if grafana_mcp else None
        self.output_dir = Path(output_dir)
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph[AgentState]:
        graph = StateGraph(AgentState)

        graph.add_node("correlate", self._correlate)
        graph.add_node("reason", self._reason)
        graph.add_node("generate_report", self._generate_report)
        graph.add_node("collect_evidence", self._collect_evidence)
        graph.add_node("render_markdown", self._render_markdown)

        graph.set_entry_point("correlate")
        graph.add_edge("correlate", "reason")
        graph.add_edge("reason", "generate_report")
        graph.add_edge("generate_report", "collect_evidence")
        graph.add_edge("collect_evidence", "render_markdown")
        graph.add_edge("render_markdown", END)

        return graph

    def compile(self) -> Any:
        """グラフをコンパイル."""
        return self.graph.compile()

    @_observe(name="rca_correlate", as_type="span")
    async def _correlate(self, state: AgentState) -> dict[str, Any]:
        """メトリクスとログの相関分析."""
        evidence_parts = []
        for mr in state.get("metrics_results", []):
            evidence_parts.append(f"## メトリクス分析結果\nクエリ: {mr.query}\n{mr.summary}")
            if mr.anomalies:
                evidence_parts.append("検出異常: " + ", ".join(mr.anomalies))
        for lr in state.get("logs_results", []):
            evidence_parts.append(f"## ログ分析結果\nクエリ: {lr.query}\n{lr.summary}")
            if lr.error_patterns:
                evidence_parts.append("エラーパターン: " + ", ".join(lr.error_patterns))

        evidence_text = "\n\n".join(evidence_parts) if evidence_parts else "調査結果なし"

        trigger_info = ""
        alert = state.get("alert")
        user_query = state.get("user_query")
        if alert is not None:
            trigger_info = (
                f"アラート: {alert.alert_name} ({alert.severity})\n"
                f"インスタンス: {alert.instance}\n"
                f"概要: {alert.summary}"
            )
        elif user_query is not None:
            trigger_info = f"ユーザ問い合わせ: {user_query.raw_input}"

        messages = [
            SystemMessage(content=RCA_AGENT_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"## トリガー\n{trigger_info}\n\n"
                    f"## 調査結果\n{evidence_text}\n\n"
                    "上記のメトリクスとログの結果を照らし合わせ、"
                    "時間的な相関や因果関係を分析してください。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        return {"messages": [response]}

    @_observe(name="rca_reason", as_type="span")
    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """根本原因の推論."""
        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "相関分析の結果に基づき、根本原因の候補を挙げてください。\n"
                    "各候補について以下を明記すること:\n"
                    "- 原因の説明\n"
                    "- 信頼度（0.0〜1.0）\n"
                    "- 根拠となる具体的なエビデンス"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        return {"messages": [response]}

    @_observe(name="rca_generate_report", as_type="span")
    async def _generate_report(self, state: AgentState) -> dict[str, Any]:
        """RCAレポートのJSON構造を生成."""
        messages = [
            *state["messages"],
            HumanMessage(
                content=(
                    "上記の分析を踏まえ、最終的なRCAレポートを以下のJSON形式で出力してください:\n"
                    "{\n"
                    '  "root_causes": [{"description": "...", "confidence": 0.9, "evidence": ["..."]}],\n'
                    '  "metrics_summary": "メトリクス分析の要約",\n'
                    '  "logs_summary": "ログ分析の要約",\n'
                    '  "recommendations": ["推奨アクション1", ...]\n'
                    "}"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)

        report = self._parse_report(response.content, state)

        return {
            "messages": [response],
            "rca_report": report,
        }

    @_observe(name="rca_collect_evidence", as_type="span")
    async def _collect_evidence(self, state: AgentState) -> dict[str, Any]:
        """レポート用のグラフ画像とログ抜粋を収集."""
        report = state.get("rca_report")
        if not report:
            return {}

        # グラフ画像の取得
        snapshots = await self._capture_panel_snapshots(state)
        report.panel_snapshots = snapshots

        # ログ抜粋の収集
        excerpts = self._collect_log_excerpts(state)
        report.log_excerpts = excerpts

        return {"rca_report": report}

    async def _capture_panel_snapshots(self, state: AgentState) -> list[PanelSnapshot]:
        """メトリクスクエリに対応するGrafanaパネル画像を取得."""
        if not self.grafana:
            logger.info("Grafana MCP未設定。パネル画像のキャプチャをスキップ。")
            return []

        snapshots: list[PanelSnapshot] = []
        plan = state.get("plan")
        time_range = plan.time_range if plan else None

        # 各メトリクスクエリに対してダッシュボードを検索し画像を取得
        for mr in state.get("metrics_results", []):
            if not mr.query:
                continue
            try:
                search_result = await self.grafana.search_dashboards(mr.query)
                dashboards = search_result.get("dashboards", [])
                if not dashboards:
                    continue

                dashboard = dashboards[0]
                uid = dashboard.get("uid", "")
                panels_result = await self.grafana.get_dashboard_panels(uid)
                panels = panels_result.get("panels", [])
                if not panels:
                    continue

                panel = panels[0]
                panel_id = panel.get("id", 0)

                # 画像を保存
                self.output_dir.mkdir(parents=True, exist_ok=True)
                image_filename = f"panel_{uid}_{panel_id}.png"
                image_path = self.output_dir / image_filename

                image_data = await self.grafana.render_panel_image(
                    dashboard_uid=uid,
                    panel_id=panel_id,
                    start=time_range.start if time_range else None,
                    end=time_range.end if time_range else None,
                )
                image_path.write_bytes(image_data)

                snapshots.append(PanelSnapshot(
                    dashboard_uid=uid,
                    panel_id=panel_id,
                    query=mr.query,
                    image_path=str(image_path),
                    caption=mr.summary[:80] if mr.summary else f"PromQL: {mr.query}",
                ))
                logger.info("パネル画像を保存: %s", image_path)

            except Exception:
                logger.warning("パネル画像の取得に失敗: query=%s", mr.query, exc_info=True)

        return snapshots

    def _collect_log_excerpts(self, state: AgentState) -> list[LogExcerpt]:
        """ログ分析結果から抜粋を収集."""
        excerpts: list[LogExcerpt] = []
        for lr in state.get("logs_results", []):
            if not lr.entries:
                continue
            # 最大20件に制限
            limited_entries = lr.entries[:20]
            excerpts.append(LogExcerpt(
                query=lr.query,
                entries=limited_entries,
                caption=f"ログ抜粋 ({len(limited_entries)}件)",
            ))
        return excerpts

    async def _render_markdown(self, state: AgentState) -> dict[str, Any]:
        """RCAレポートをMarkdownにレンダリングし保存."""
        report = state.get("rca_report")
        if not report:
            return {}

        report.markdown = render_rca_markdown(report)

        # ファイルに保存
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = report.created_at.strftime("%Y%m%d_%H%M%S")
        md_path = self.output_dir / f"rca_report_{ts}.md"
        md_path.write_text(report.markdown, encoding="utf-8")
        logger.info("RCAレポートを保存: %s", md_path)

        return {"rca_report": report}

    # ---- パーサー ----

    def _parse_report(self, content: str, state: AgentState) -> RCAReport:
        """LLM出力からRCAレポートをパース."""
        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            root_causes = [RootCause(**rc) for rc in data.get("root_causes", [])]
        except (json.JSONDecodeError, ValueError):
            logger.warning("RCAレポートのパースに失敗。LLM出力をそのまま使用。")
            root_causes = [RootCause(description=content, confidence=0.5)]
            data = {}

        return RCAReport(
            trigger_type=state["trigger_type"],
            alert=state.get("alert"),
            user_query=state.get("user_query"),
            root_causes=root_causes,
            metrics_summary=data.get("metrics_summary", ""),
            logs_summary=data.get("logs_summary", ""),
            recommendations=data.get("recommendations", []),
        )

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
