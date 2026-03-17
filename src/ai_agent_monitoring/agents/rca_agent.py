"""RCA Agent — 根本原因分析."""

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from ai_agent_monitoring.agents.prompts import RCA_AGENT_SYSTEM_PROMPT
from ai_agent_monitoring.core.confidence import compute_confidence
from ai_agent_monitoring.core.json_repair import extract_json, repair_truncated_json
from ai_agent_monitoring.core.models import (
    LogExcerpt,
    PanelSnapshot,
    RCAReport,
    RCAReportSchema,
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

    def _observe(func: Any = None, **kwargs: Any) -> Any:
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
        use_structured_output: bool = True,
    ) -> None:
        self.llm = llm
        self.grafana = GrafanaMCPTool(grafana_mcp) if grafana_mcp else None
        self.output_dir = Path(output_dir)
        self._use_structured_output = use_structured_output
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

    @staticmethod
    def _format_observations(results: list, label: str) -> list[str]:
        """Result の observations をフォーマットしてエビデンスに追加."""
        parts: list[str] = []
        for r in results:
            if hasattr(r, "observations") and r.observations:
                for obs in r.observations:
                    parts.append(f"### [{label}] {obs.tool_name}({obs.tool_input})\n{obs.observation}")
        return parts

    @_observe(name="rca_correlate", as_type="span")
    async def _correlate(self, state: AgentState) -> dict[str, Any]:
        """メトリクスとログの相関分析."""
        evidence_parts = []

        metrics_results = state.get("metrics_results", [])
        logs_results = state.get("logs_results", [])
        k8s_results = state.get("k8s_results", [])

        # 各エージェントのsummary
        for mr in metrics_results:
            evidence_parts.append(f"## メトリクス分析結果\nクエリ: {mr.query}\n{mr.summary}")
            if mr.anomalies:
                evidence_parts.append("検出異常: " + ", ".join(mr.anomalies))
        for lr in logs_results:
            evidence_parts.append(f"## ログ分析結果\nクエリ: {lr.query}\n{lr.summary}")
            if lr.error_patterns:
                evidence_parts.append("エラーパターン: " + ", ".join(lr.error_patterns))
        for kr in k8s_results:
            evidence_parts.append(f"## Kubernetes分析結果\n{kr.summary}")
            if kr.anomalies:
                evidence_parts.append("K8s異常: " + ", ".join(kr.anomalies))
            if kr.events:
                evidence_parts.append("K8sイベント: " + ", ".join(kr.events[:10]))

        # ツール実行ペアごとの観察結果（根拠の明示）
        obs_parts = (
            self._format_observations(metrics_results, "Metrics")
            + self._format_observations(logs_results, "Logs")
            + self._format_observations(k8s_results, "K8s")
        )
        if obs_parts:
            evidence_parts.append("## ツール実行の観察結果（根拠）\n" + "\n\n".join(obs_parts))

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
        """RCAレポートのJSON構造を生成.

        LLM Structured Output を優先し、失敗時はテキストパースにフォールバック。
        """
        report_prompt = (
            "上記の分析を踏まえ、最終的なRCAレポートを生成してください。\n\n"
            "## 出力フィールド\n"
            "- root_causes: 根本原因のリスト（description, confidence 0.0〜1.0, evidence）\n"
            "- metrics_summary: メトリクス分析の要約（Markdown形式）\n"
            "- logs_summary: ログ分析の要約（Markdown形式）\n"
            "- k8s_summary: Kubernetes分析の要約（Markdown形式）\n"
            "- recommendations: 推奨アクションのリスト\n\n"
            "## summaryフィールドのフォーマット規則\n"
            "各summaryフィールドはMarkdown形式で記述してください:\n"
            "- PromQL/LogQLクエリは ```promql や ```logql のコードブロックで囲む\n"
            "- メトリクス値は `値` のインラインコードで囲む\n"
            "- 箇条書き（- ）で項目を分ける\n"
            "- 改行（\\n）で段落を分ける"
        )
        messages = [*state["messages"], HumanMessage(content=report_prompt)]

        # Structured Output を試行（無効化されている場合はスキップ）
        schema_result = await self._invoke_structured_report(messages) if self._use_structured_output else None
        if schema_result is not None:
            report = self._build_report_from_schema(schema_result, state)
        else:
            # フォールバック: テキスト生成 → 手動パース
            response = await self.llm.ainvoke(messages)
            report = self._parse_report(response.content, state)
            return {"messages": [response], "rca_report": report}

        return {"rca_report": report}

    async def _invoke_structured_report(self, messages: list[Any]) -> RCAReportSchema | None:
        """Structured Output で RCAReportSchema を取得.失敗時は None."""
        try:
            structured_llm = self.llm.with_structured_output(RCAReportSchema)
            result = await structured_llm.ainvoke(messages)
            if isinstance(result, RCAReportSchema):
                logger.info("Structured output で RCA レポートを取得しました")
                return result
            if isinstance(result, dict):
                logger.info("Structured output が dict を返却、RCAReportSchema に変換")
                return RCAReportSchema(**result)
            raise TypeError(f"Unexpected type from structured output: {type(result)}")
        except Exception as e:
            logger.warning(
                "Structured output 失敗、テキストパースにフォールバック: %s: %s",
                type(e).__name__,
                e,
            )
            return None

    def _build_report_from_schema(self, schema: RCAReportSchema, state: AgentState) -> RCAReport:
        """RCAReportSchema から RCAReport を構築."""
        root_causes = []
        for rc_schema in schema.root_causes:
            conf = rc_schema.confidence
            if conf > 1.0:
                conf = conf / 100.0
            conf = max(0.0, min(1.0, conf))
            root_causes.append(
                RootCause(description=rc_schema.description, confidence=conf, evidence=rc_schema.evidence)
            )

        agent_tool_outputs = self._collect_agent_tool_outputs(state)

        for rc in root_causes:
            details = compute_confidence(rc, state)
            rc.confidence = details.final_confidence
            rc.confidence_details = details

        return RCAReport(
            trigger_type=state["trigger_type"],
            alert=state.get("alert"),
            user_query=state.get("user_query"),
            root_causes=root_causes,
            metrics_summary=schema.metrics_summary,
            logs_summary=schema.logs_summary,
            k8s_summary=schema.k8s_summary,
            recommendations=schema.recommendations,
            agent_tool_outputs=agent_tool_outputs,
        )

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

                snapshots.append(
                    PanelSnapshot(
                        dashboard_uid=uid,
                        panel_id=panel_id,
                        query=mr.query,
                        image_path=str(image_path),
                        caption=mr.summary[:80] if mr.summary else f"PromQL: {mr.query}",
                    )
                )
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
            excerpts.append(
                LogExcerpt(
                    query=lr.query,
                    entries=limited_entries,
                    caption=f"ログ抜粋 ({len(limited_entries)}件)",
                )
            )
        return excerpts

    async def _generate_search_keywords(self, report: RCAReport) -> str:
        """レポートから英語検索キーワードを生成."""
        summary = (
            f"Root causes: {'; '.join(rc.description for rc in report.root_causes[:5])}\n"
            f"Metrics: {report.metrics_summary[:500]}\n"
            f"Logs: {report.logs_summary[:500]}\n"
            f"K8s: {report.k8s_summary[:500]}\n"
            f"Recommendations: {'; '.join(report.recommendations[:5])}"
        )
        messages = [
            SystemMessage(
                content=(
                    "Extract English search keywords from this RCA report summary. "
                    "Include: metric names, error types, service names, resource types, "
                    "symptoms, root cause categories. "
                    "Output ONLY space-separated English keywords, no explanation."
                )
            ),
            HumanMessage(content=summary),
        ]
        try:
            response = await self.llm.ainvoke(messages)
            result: str = response.content.strip()
            return result
        except Exception:
            logger.warning("Failed to generate search keywords, using empty")
            return ""

    async def _render_markdown(self, state: AgentState) -> dict[str, Any]:
        """RCAレポートをMarkdownにレンダリングし保存."""
        report = state.get("rca_report")
        if not report:
            return {}

        report.markdown = render_rca_markdown(report)
        report.search_keywords_en = await self._generate_search_keywords(report)

        # ファイルに保存
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = report.created_at.strftime("%Y%m%d_%H%M%S")
        md_path = self.output_dir / f"rca_report_{ts}.md"
        md_path.write_text(report.markdown, encoding="utf-8")
        logger.info("RCAレポートを保存: %s", md_path)

        return {"rca_report": report}

    # ---- パーサー ----

    @staticmethod
    def _collect_agent_tool_outputs(state: AgentState) -> dict[str, list[str]]:
        """各エージェントの tool_outputs を集約."""
        agent_tool_outputs: dict[str, list[str]] = {}
        for mr in state.get("metrics_results", []):
            if mr.tool_outputs:
                agent_tool_outputs.setdefault("metrics", []).extend(mr.tool_outputs)
        for lr in state.get("logs_results", []):
            if lr.tool_outputs:
                agent_tool_outputs.setdefault("logs", []).extend(lr.tool_outputs)
        for kr in state.get("k8s_results", []):
            if kr.tool_outputs:
                agent_tool_outputs.setdefault("k8s", []).extend(kr.tool_outputs)
        return agent_tool_outputs

    def _parse_report(self, content: str, state: AgentState) -> RCAReport:
        """LLM出力からRCAレポートをパース（フォールバック用）."""
        try:
            json_str = extract_json(content)
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                repaired = repair_truncated_json(json_str)
                data = json.loads(repaired)
            root_causes = []
            for rc in data.get("root_causes", []):
                conf = float(rc.get("confidence", 0.5))
                if conf > 1.0:
                    conf = conf / 100.0
                rc["confidence"] = max(0.0, min(1.0, conf))
                root_causes.append(RootCause(**rc))
        except Exception:
            logger.warning("RCAレポートのパースに失敗。LLM出力をそのまま使用。content=%.500s", content, exc_info=True)
            root_causes = [RootCause(description=content, confidence=0.5)]
            data = {}

        agent_tool_outputs = self._collect_agent_tool_outputs(state)

        for rc in root_causes:
            details = compute_confidence(rc, state)
            rc.confidence = details.final_confidence
            rc.confidence_details = details

        return RCAReport(
            trigger_type=state["trigger_type"],
            alert=state.get("alert"),
            user_query=state.get("user_query"),
            root_causes=root_causes,
            metrics_summary=data.get("metrics_summary", ""),
            logs_summary=data.get("logs_summary", ""),
            k8s_summary=data.get("k8s_summary", ""),
            recommendations=data.get("recommendations", []),
            agent_tool_outputs=agent_tool_outputs,
        )
