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
        response = MagicMock()
        response.content = "テスト回答: CPUが高い原因はループ処理でした。"
        llm.ainvoke = AsyncMock(return_value=response)
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

        mock_llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_no_results(self, mock_llm, mock_store):
        mock_store.search.return_value = []

        agent = ReportSearchAgent(llm=mock_llm, report_store=mock_store)
        result = await agent.search_and_answer("存在しないクエリ")

        assert "見つかりませんでした" in result.answer
        assert result.results == []
        assert result.total_reports == 5
        mock_llm.ainvoke.assert_not_called()

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
