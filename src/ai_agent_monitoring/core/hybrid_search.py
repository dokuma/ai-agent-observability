"""BM25 + Vector のハイブリッド検索（RRF 統合）."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ai_agent_monitoring.core.models import StoredRCAReport
from ai_agent_monitoring.core.report_store import ReportStore

if TYPE_CHECKING:
    from ai_agent_monitoring.core.vector_store import VectorSearchResult, VectorStore

logger = logging.getLogger(__name__)


class HybridSearcher:
    """ReportStore (BM25) と VectorStore を RRF で統合する検索エンジン."""

    def __init__(
        self,
        report_store: ReportStore,
        vector_store: VectorStore | None,
        rrf_k: int = 60,
    ) -> None:
        self._report_store = report_store
        self._vector_store = vector_store
        self._rrf_k = rrf_k

    async def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[tuple[StoredRCAReport, float, list[str]]]:
        """BM25 + Vector のハイブリッド検索.

        戻り値は ReportStore.search() と同じ型を維持。
        """
        bm25_results = self._report_store.search(query, top_k=top_k)
        logger.info(
            "Hybrid search: BM25 returned %d results (top_score=%.3f)",
            len(bm25_results),
            bm25_results[0][1] if bm25_results else 0,
        )

        vector_results = await self._vector_search(query, top_k)
        logger.info(
            "Hybrid search: Vector returned %d results (top_score=%.3f)",
            len(vector_results),
            vector_results[0].score if vector_results else 0,
        )

        if not vector_results:
            return bm25_results

        bm25_ranks: dict[str, int] = {}
        bm25_data: dict[str, tuple[StoredRCAReport, float, list[str]]] = {}
        for rank, (report, score, highlights) in enumerate(bm25_results):
            bm25_ranks[report.id] = rank
            bm25_data[report.id] = (report, score, highlights)

        vector_ranks: dict[str, int] = {}
        for rank, vr in enumerate(vector_results):
            vector_ranks[vr.doc_id] = rank

        all_ids = set(bm25_ranks.keys()) | set(vector_ranks.keys())
        rrf_scores: dict[str, float] = {}
        for doc_id in all_ids:
            score = 0.0
            if doc_id in bm25_ranks:
                score += 1.0 / (self._rrf_k + bm25_ranks[doc_id])
            if doc_id in vector_ranks:
                score += 1.0 / (self._rrf_k + vector_ranks[doc_id])
            rrf_scores[doc_id] = score

        sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)[:top_k]
        logger.info(
            "Hybrid search: RRF merged %d unique docs, returning top %d",
            len(all_ids),
            len(sorted_ids),
        )

        results: list[tuple[StoredRCAReport, float, list[str]]] = []
        for doc_id in sorted_ids:
            if doc_id in bm25_data:
                report, _, highlights = bm25_data[doc_id]
            else:
                maybe_report = self._report_store.get_report(doc_id)
                if maybe_report is None:
                    continue
                report = maybe_report
                highlights = []
            results.append((report, rrf_scores[doc_id], highlights))

        return results

    async def _vector_search(self, query: str, top_k: int) -> list[VectorSearchResult]:
        """ベクトル検索（失敗時は空リスト）."""
        if self._vector_store is None:
            return []
        try:
            return await self._vector_store.search(query, top_k=top_k)
        except Exception:
            logger.warning("Vector search failed, falling back to BM25 only", exc_info=True)
            return []
