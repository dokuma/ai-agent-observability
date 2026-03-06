"""HybridSearcher のテスト."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_agent_monitoring.core.hybrid_search import HybridSearcher
from ai_agent_monitoring.core.models import (
    Alert,
    RCAReport,
    RootCause,
    Severity,
    StoredRCAReport,
    TriggerType,
)
from ai_agent_monitoring.core.vector_store import VectorSearchResult


def _make_stored(report_id: str, investigation_id: str = "inv-001") -> StoredRCAReport:
    report = RCAReport(
        trigger_type=TriggerType.ALERT,
        alert=Alert(
            alert_name="TestAlert",
            severity=Severity.WARNING,
            instance="web-01",
            summary="Test summary",
            starts_at="2026-01-15T10:00:00Z",
        ),
        root_causes=[RootCause(description="Root cause", confidence=0.8)],
        metrics_summary="metrics",
        logs_summary="logs",
        recommendations=["fix it"],
    )
    return StoredRCAReport(
        id=report_id,
        investigation_id=investigation_id,
        report=report,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_report_store():
    store = MagicMock()
    return store


@pytest.fixture
def mock_vector_store():
    vs = MagicMock()
    vs.search = AsyncMock(return_value=[])
    return vs


class TestHybridSearcherRRF:
    @pytest.mark.asyncio
    async def test_bm25_only_when_no_vector_store(self, mock_report_store):
        r1 = _make_stored("r1")
        mock_report_store.search.return_value = [(r1, 2.0, ["highlight"])]

        searcher = HybridSearcher(mock_report_store, vector_store=None)
        results = await searcher.search("test query")

        assert len(results) == 1
        assert results[0][0].id == "r1"
        assert results[0][1] == 2.0

    @pytest.mark.asyncio
    async def test_bm25_only_when_vector_fails(self, mock_report_store, mock_vector_store):
        r1 = _make_stored("r1")
        mock_report_store.search.return_value = [(r1, 2.0, ["highlight"])]
        mock_vector_store.search = AsyncMock(side_effect=Exception("connection error"))

        searcher = HybridSearcher(mock_report_store, mock_vector_store)
        results = await searcher.search("test query")

        assert len(results) == 1
        assert results[0][0].id == "r1"

    @pytest.mark.asyncio
    async def test_rrf_fusion_both_sources(self, mock_report_store, mock_vector_store):
        """BM25 と Vector の両方から結果がある場合の RRF 統合."""
        r1 = _make_stored("r1")
        r2 = _make_stored("r2")
        r3 = _make_stored("r3")

        # BM25: r1(rank0), r2(rank1)
        mock_report_store.search.return_value = [
            (r1, 3.0, ["h1"]),
            (r2, 1.5, ["h2"]),
        ]
        # Vector: r2(rank0), r3(rank1)
        mock_vector_store.search = AsyncMock(
            return_value=[
                VectorSearchResult(doc_id="r2", score=0.95, payload={}),
                VectorSearchResult(doc_id="r3", score=0.80, payload={}),
            ]
        )
        mock_report_store.get_report.return_value = r3

        searcher = HybridSearcher(mock_report_store, mock_vector_store, rrf_k=60)
        results = await searcher.search("test query", top_k=5)

        ids = [r[0].id for r in results]
        # r2 appears in both → highest RRF score
        assert ids[0] == "r2"
        # r2 RRF = 1/(60+1) + 1/(60+0) = ~0.033
        r2_score = results[0][1]
        assert r2_score > 0.03

    @pytest.mark.asyncio
    async def test_rrf_vector_only_doc(self, mock_report_store, mock_vector_store):
        """ベクトル検索のみに存在するドキュメントも結果に含まれる."""
        r_new = _make_stored("r_new")
        mock_report_store.search.return_value = []
        mock_vector_store.search = AsyncMock(
            return_value=[
                VectorSearchResult(doc_id="r_new", score=0.9, payload={}),
            ]
        )
        mock_report_store.get_report.return_value = r_new

        searcher = HybridSearcher(mock_report_store, mock_vector_store)
        results = await searcher.search("test")

        assert len(results) == 1
        assert results[0][0].id == "r_new"

    @pytest.mark.asyncio
    async def test_both_empty(self, mock_report_store, mock_vector_store):
        mock_report_store.search.return_value = []
        mock_vector_store.search = AsyncMock(return_value=[])

        searcher = HybridSearcher(mock_report_store, mock_vector_store)
        results = await searcher.search("nothing")

        assert results == []

    @pytest.mark.asyncio
    async def test_vector_doc_not_in_sqlite_skipped(self, mock_report_store, mock_vector_store):
        """ベクトル検索結果の doc_id が SQLite に存在しない場合はスキップ."""
        mock_report_store.search.return_value = []
        mock_vector_store.search = AsyncMock(
            return_value=[
                VectorSearchResult(doc_id="ghost", score=0.9, payload={}),
            ]
        )
        mock_report_store.get_report.return_value = None

        searcher = HybridSearcher(mock_report_store, mock_vector_store)
        results = await searcher.search("test")

        assert results == []
