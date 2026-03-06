"""ReportSearchAgentのテスト."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_agent_monitoring.agents.report_search_agent import ReportSearchAgent
from ai_agent_monitoring.core.models import (
    Alert,
    RCAReport,
    RootCause,
    Severity,
    StoredRCAReport,
    TriggerType,
    UserQuery,
)


def _make_stored_report(
    report_id: str = "abc123def456",
    investigation_id: str = "inv-001",
    trigger_type: TriggerType = TriggerType.ALERT,
) -> StoredRCAReport:
    report = RCAReport(
        trigger_type=trigger_type,
        alert=Alert(
            alert_name="HighCPU",
            severity=Severity.WARNING,
            instance="web-01",
            summary="CPU high",
            starts_at="2026-01-15T10:00:00Z",
        )
        if trigger_type == TriggerType.ALERT
        else None,
        user_query=UserQuery(raw_input="test query") if trigger_type == TriggerType.USER_QUERY else None,
        root_causes=[
            RootCause(description="High CPU due to infinite loop", confidence=0.9),
        ],
        metrics_summary="CPU usage at 95%",
        logs_summary="Error loop detected",
        recommendations=["Fix the loop", "Add circuit breaker"],
    )
    return StoredRCAReport(
        id=report_id,
        investigation_id=investigation_id,
        report=report,
        created_at=datetime.now(UTC),
    )


class TestReportSearchAgent:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        # ainvoke is called for both query translation and answer generation
        translate_response = MagicMock()
        translate_response.content = "high CPU cause loop"
        answer_response = MagicMock()
        answer_response.content = "テスト回答: CPUが高い原因はループ処理でした。"
        llm.ainvoke = AsyncMock(side_effect=[translate_response, answer_response])
        return llm

    @pytest.fixture
    def mock_store(self):
        store = MagicMock()
        store.count.return_value = 5
        return store

    @pytest.mark.asyncio
    async def test_search_with_results(self, mock_llm, mock_store):
        stored = _make_stored_report()
        mock_store.search.return_value = [
            (stored, 2.5, ["CPU usage at 95%"]),
        ]

        agent = ReportSearchAgent(llm=mock_llm, report_store=mock_store)
        result = await agent.search_and_answer("CPUが高い原因は？")

        assert result.answer == "テスト回答: CPUが高い原因はループ処理でした。"
        assert len(result.results) == 1
        assert result.results[0].report_id == "abc123def456"
        assert result.results[0].trigger_type == "alert"
        assert result.results[0].alert_name == "HighCPU"
        assert result.total_reports == 5

        # 1st call: query translation, 2nd call: answer generation
        assert mock_llm.ainvoke.call_count == 2

    @pytest.mark.asyncio
    async def test_search_no_results(self, mock_llm, mock_store):
        mock_store.search.return_value = []

        agent = ReportSearchAgent(llm=mock_llm, report_store=mock_store)
        result = await agent.search_and_answer("存在しないクエリ")

        assert "見つかりませんでした" in result.answer
        assert result.results == []
        assert result.total_reports == 5
        # Only query translation is called (1 time), answer generation is skipped
        assert mock_llm.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_search_user_query_trigger(self, mock_llm, mock_store):
        stored = _make_stored_report(
            trigger_type=TriggerType.USER_QUERY,
        )
        mock_store.search.return_value = [
            (stored, 1.5, []),
        ]

        agent = ReportSearchAgent(llm=mock_llm, report_store=mock_store)
        result = await agent.search_and_answer("テスト")

        assert result.results[0].trigger_type == "user_query"
        assert result.results[0].alert_name is None

    @pytest.mark.asyncio
    async def test_translate_query_failure_falls_back(self, mock_store):
        """クエリ変換が失敗した場合、元のクエリで検索する."""
        llm = MagicMock()
        # Translation fails, then answer generation succeeds
        answer_response = MagicMock()
        answer_response.content = "回答"
        llm.ainvoke = AsyncMock(side_effect=[Exception("LLM error"), answer_response])

        stored = _make_stored_report()
        mock_store.search.return_value = [(stored, 1.0, [])]

        agent = ReportSearchAgent(llm=llm, report_store=mock_store)
        result = await agent.search_and_answer("CPUが高い")

        # Translation failed, so search is called with the original query only
        mock_store.search.assert_called_once_with("CPUが高い", top_k=5)
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_fallback_search_on_empty_results(self, mock_store):
        """英語変換+元クエリで0件→元クエリのみで再検索."""
        llm = MagicMock()
        translate_response = MagicMock()
        translate_response.content = "cpu high"
        answer_response = MagicMock()
        answer_response.content = "回答"
        llm.ainvoke = AsyncMock(side_effect=[translate_response, answer_response])

        stored = _make_stored_report()
        # First search (combined) returns empty, second (original) returns results
        mock_store.search.side_effect = [[], [(stored, 1.0, [])]]
        mock_store.count.return_value = 5

        agent = ReportSearchAgent(llm=llm, report_store=mock_store)
        result = await agent.search_and_answer("CPUが高い")

        assert mock_store.search.call_count == 2
        assert len(result.results) == 1


class TestReportSearchAgentWithHybridSearcher:
    @pytest.fixture
    def mock_hybrid_searcher(self):
        searcher = MagicMock()
        searcher.search = AsyncMock()
        return searcher

    @pytest.mark.asyncio
    async def test_uses_hybrid_searcher_when_provided(self, mock_hybrid_searcher):
        stored = _make_stored_report()
        mock_hybrid_searcher.search.return_value = [(stored, 0.05, ["highlight"])]

        llm = MagicMock()
        translate_resp = MagicMock()
        translate_resp.content = "high CPU"
        answer_resp = MagicMock()
        answer_resp.content = "ハイブリッド検索回答"
        llm.ainvoke = AsyncMock(side_effect=[translate_resp, answer_resp])

        store = MagicMock()
        store.count.return_value = 10

        agent = ReportSearchAgent(llm=llm, report_store=store, hybrid_searcher=mock_hybrid_searcher)
        result = await agent.search_and_answer("CPUが高い")

        mock_hybrid_searcher.search.assert_called_once()
        store.search.assert_not_called()
        assert result.answer == "ハイブリッド検索回答"

    @pytest.mark.asyncio
    async def test_falls_back_without_hybrid_searcher(self):
        stored = _make_stored_report()
        llm = MagicMock()
        translate_resp = MagicMock()
        translate_resp.content = "high CPU"
        answer_resp = MagicMock()
        answer_resp.content = "BM25回答"
        llm.ainvoke = AsyncMock(side_effect=[translate_resp, answer_resp])

        store = MagicMock()
        store.count.return_value = 5
        store.search.return_value = [(stored, 2.0, [])]

        agent = ReportSearchAgent(llm=llm, report_store=store, hybrid_searcher=None)
        result = await agent.search_and_answer("test")

        store.search.assert_called_once()
        assert result.answer == "BM25回答"
