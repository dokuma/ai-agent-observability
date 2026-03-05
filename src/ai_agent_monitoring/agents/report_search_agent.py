"""RCAレポート検索エージェント."""

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ai_agent_monitoring.agents.prompts import REPORT_SEARCH_SYSTEM_PROMPT
from ai_agent_monitoring.api.schemas import ReportSearchResponse, ReportSearchResult
from ai_agent_monitoring.core.report_store import ReportStore

logger = logging.getLogger(__name__)


class ReportSearchAgent:
    """過去のRCAレポートを検索し、LLMで回答を生成するエージェント."""

    def __init__(self, llm: Any, report_store: ReportStore) -> None:
        self._llm = llm
        self._report_store = report_store

    async def _translate_query_to_keywords(self, query: str) -> str:
        """ユーザクエリから英語検索キーワードを生成."""
        messages = [
            SystemMessage(
                content=(
                    "Convert this monitoring/infrastructure query to English search keywords. "
                    "Include: metric names, error types, service names, resource types, symptoms. "
                    "Output ONLY space-separated English keywords, no explanation."
                )
            ),
            HumanMessage(content=query),
        ]
        try:
            response = await self._llm.ainvoke(messages)
            result: str = response.content.strip()
            return result
        except Exception:
            logger.warning("Failed to translate query to English keywords")
            return ""

    async def search_and_answer(self, query: str, top_k: int = 5) -> ReportSearchResponse:
        """クエリに基づいてレポートを検索し、LLMで回答を生成する."""
        total = self._report_store.count()

        en_query = await self._translate_query_to_keywords(query)
        combined_query = f"{en_query} {query}".strip() if en_query else query
        results = self._report_store.search(combined_query, top_k=top_k)

        if not results and en_query:
            results = self._report_store.search(query, top_k=top_k)

        if not results:
            return ReportSearchResponse(
                answer="該当するRCAレポートが見つかりませんでした。",
                results=[],
                total_reports=total,
            )

        # Build context from search results
        context_parts: list[str] = []
        search_results: list[ReportSearchResult] = []
        for i, (report, score, highlights) in enumerate(results, 1):
            rca = report.report
            root_causes_summary = "; ".join(rc.description for rc in rca.root_causes[:3]) if rca.root_causes else "不明"

            # agent_tool_outputs のコンテキスト（各ソース先頭500文字）
            tool_output_context = ""
            if rca.agent_tool_outputs:
                tool_parts: list[str] = []
                for source, outputs in rca.agent_tool_outputs.items():
                    combined = " ".join(outputs)[:500]
                    if combined:
                        tool_parts.append(f"  {source}: {combined}")
                if tool_parts:
                    tool_output_context = "\nツール実行結果:\n" + "\n".join(tool_parts) + "\n"

            context_parts.append(
                f"--- レポート {i} (ID: {report.id}, スコア: {score:.2f}) ---\n"
                f"作成日時: {report.created_at.isoformat()}\n"
                f"根本原因: {root_causes_summary}\n"
                f"メトリクス: {rca.metrics_summary or 'なし'}\n"
                f"ログ: {rca.logs_summary or 'なし'}\n"
                f"K8s: {rca.k8s_summary or 'なし'}\n"
                f"推奨事項: {'; '.join(rca.recommendations[:3]) if rca.recommendations else 'なし'}\n"
                f"{tool_output_context}"
            )

            # Determine trigger info
            alert_name = None
            trigger_type = "user_query"
            if rca.alert:
                alert_name = rca.alert.labels.get("alertname", rca.alert.alert_name)
                trigger_type = "alert"

            search_results.append(
                ReportSearchResult(
                    report_id=report.id,
                    investigation_id=report.investigation_id,
                    score=score,
                    trigger_type=trigger_type,
                    alert_name=alert_name,
                    root_causes_summary=root_causes_summary,
                    created_at=report.created_at,
                    highlights=highlights,
                )
            )

        context = "\n".join(context_parts)

        # Generate answer with LLM
        messages = [
            SystemMessage(content=REPORT_SEARCH_SYSTEM_PROMPT),
            HumanMessage(content=f"## 検索クエリ\n{query}\n\n## 検索結果\n{context}"),
        ]

        response = await self._llm.ainvoke(messages)
        answer = response.content if hasattr(response, "content") else str(response)

        return ReportSearchResponse(
            answer=answer,
            results=search_results,
            total_reports=total,
        )
