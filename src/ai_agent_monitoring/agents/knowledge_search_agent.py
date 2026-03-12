"""ナレッジ検索エージェント — RCAレポートと過去の観測データを統合検索."""

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from ai_agent_monitoring.agents.prompts import KNOWLEDGE_SEARCH_SYSTEM_PROMPT
from ai_agent_monitoring.api.schemas import ReportSearchResponse, ReportSearchResult
from ai_agent_monitoring.core.report_store import ReportStore

if TYPE_CHECKING:
    from ai_agent_monitoring.core.hybrid_search import HybridSearcher

logger = logging.getLogger(__name__)

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe
except ImportError:

    def _observe(func: Any = None, **kwargs: Any) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func

        def decorator(f: Any) -> Any:
            return f

        return decorator


class KnowledgeSearchAgent:
    """過去のRCAレポートと観測データを検索し、LLMで回答を生成するエージェント."""

    def __init__(self, llm: Any, report_store: ReportStore, hybrid_searcher: "HybridSearcher | None" = None) -> None:
        self._llm = llm
        self._report_store = report_store
        self._hybrid_searcher = hybrid_searcher

    @_observe(name="knowledge_search_translate_keywords", as_type="span")
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

    @_observe(name="knowledge_search_and_answer", as_type="span")
    async def search_and_answer(
        self, query: str, top_k: int = 5, observation_context: str = "", query_history: str = ""
    ) -> ReportSearchResponse:
        """クエリに基づいてRCAレポートと過去の観測データを検索し、LLMで回答を生成する."""
        total = self._report_store.count()
        logger.info("Knowledge search started: query=%s, total_reports=%d", query[:100], total)

        en_query = await self._translate_query_to_keywords(query)
        combined_query = f"{en_query} {query}".strip() if en_query else query
        logger.info("Translated keywords: %s", en_query[:200] if en_query else "(empty)")

        if self._hybrid_searcher:
            results = await self._hybrid_searcher.search(combined_query, top_k=top_k)
        else:
            results = self._report_store.search(combined_query, top_k=top_k)
        logger.info("Search returned %d results", len(results))

        if not results and en_query:
            if self._hybrid_searcher:
                results = await self._hybrid_searcher.search(query, top_k=top_k)
            else:
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

            # agent_tool_outputs のコンテキスト（各ソース先頭1500文字）
            tool_output_context = ""
            if rca.agent_tool_outputs:
                tool_parts: list[str] = []
                for source, outputs in rca.agent_tool_outputs.items():
                    combined = " ".join(outputs)[:1500]
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
        logger.info("Generating LLM answer for knowledge search")
        human_content = f"## 検索クエリ\n{query}\n\n## 検索結果\n{context}"
        if observation_context:
            human_content += (
                "\n\n## 過去の類似観測データ\n"
                "以下は過去の調査で取得された類似の観測データです。"
                "回答の補足情報として活用してください:\n" + observation_context
            )
        if query_history:
            human_content += "\n\n## クエリ実行履歴\n以下は過去の調査で実行されたクエリの一覧です:\n" + query_history
        messages = [
            SystemMessage(content=KNOWLEDGE_SEARCH_SYSTEM_PROMPT),
            HumanMessage(content=human_content),
        ]

        response = await self._llm.ainvoke(messages)
        raw_content = response.content if hasattr(response, "content") else str(response)

        # LLMプロバイダによっては content がリスト形式のコンテンツブロックで返る場合がある
        # 例: [{"type": "text", "text": "..."}]
        if isinstance(raw_content, list):
            text_parts = []
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            answer = "\n".join(text_parts)
        else:
            answer = str(raw_content)

        # 空や JSON 構造だけの回答はフォールバック
        stripped = answer.strip()
        if not stripped or stripped in ("{}", "[]", "null"):
            logger.warning("Knowledge search returned empty/JSON-only answer: %r", stripped[:100])
            answer = (
                "過去のRCAレポートから情報が見つかりましたが、"
                "回答の生成に失敗しました。別のキーワードでお試しください。"
            )

        logger.info("Knowledge search completed: answer_length=%d", len(answer))

        return ReportSearchResponse(
            answer=answer,
            results=search_results,
            total_reports=total,
        )
