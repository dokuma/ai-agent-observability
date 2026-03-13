"""KnowledgeSearchAgentのテスト."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_agent_monitoring.agents.knowledge_search_agent import KnowledgeSearchAgent
from ai_agent_monitoring.core.vector_store import VectorSearchResult


def _make_payload(
    report_id: str = "abc123def456",
    investigation_id: str = "inv-001",
    trigger_type: str = "alert",
) -> dict:
    """Qdrant ペイロードを生成."""
    import json

    report_data = {
        "trigger_type": trigger_type,
        "root_causes": [{"description": "High CPU due to infinite loop", "confidence": 0.9, "evidence": []}],
        "metrics_summary": "CPU usage at 95%",
        "logs_summary": "Error loop detected",
        "recommendations": ["Fix the loop", "Add circuit breaker"],
        "search_keywords_en": "",
    }
    if trigger_type == "alert":
        report_data["alert"] = {
            "alert_name": "HighCPU",
            "severity": "warning",
            "instance": "web-01",
            "summary": "CPU high",
            "starts_at": "2026-01-15T10:00:00Z",
        }
    elif trigger_type == "user_query":
        report_data["user_query"] = {"raw_input": "test query"}

    return {
        "report_id": report_id,
        "investigation_id": investigation_id,
        "trigger_type": trigger_type,
        "created_at_ts": datetime.now(UTC).timestamp(),
        "report_json": json.dumps(report_data),
    }


def _make_vector_result(
    report_id: str = "abc123def456",
    score: float = 0.85,
    trigger_type: str = "alert",
) -> VectorSearchResult:
    return VectorSearchResult(
        doc_id=report_id,
        score=score,
        payload=_make_payload(report_id=report_id, trigger_type=trigger_type),
    )


class TestKnowledgeSearchAgent:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        translate_response = MagicMock()
        translate_response.content = "high CPU cause loop"
        answer_response = MagicMock()
        answer_response.content = "テスト回答: CPUが高い原因はループ処理でした。"
        llm.ainvoke = AsyncMock(side_effect=[translate_response, answer_response])
        return llm

    @pytest.fixture
    def mock_vector_store(self):
        store = AsyncMock()
        store.count = AsyncMock(return_value=5)
        return store

    @pytest.mark.asyncio
    async def test_search_with_results(self, mock_llm, mock_vector_store):
        vr = _make_vector_result()
        mock_vector_store.search = AsyncMock(return_value=[vr])

        agent = KnowledgeSearchAgent(llm=mock_llm, vector_store=mock_vector_store)
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
    async def test_search_no_results(self, mock_llm, mock_vector_store):
        mock_vector_store.search = AsyncMock(return_value=[])

        agent = KnowledgeSearchAgent(llm=mock_llm, vector_store=mock_vector_store)
        result = await agent.search_and_answer("存在しないクエリ")

        assert "見つかりませんでした" in result.answer
        assert result.results == []
        assert result.total_reports == 5
        # Only query translation is called (1 time), answer generation is skipped
        assert mock_llm.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_search_user_query_trigger(self, mock_llm, mock_vector_store):
        vr = _make_vector_result(trigger_type="user_query")
        mock_vector_store.search = AsyncMock(return_value=[vr])

        agent = KnowledgeSearchAgent(llm=mock_llm, vector_store=mock_vector_store)
        result = await agent.search_and_answer("テスト")

        assert result.results[0].trigger_type == "user_query"
        assert result.results[0].alert_name is None

    @pytest.mark.asyncio
    async def test_translate_query_failure_falls_back(self, mock_vector_store):
        """クエリ変換が失敗した場合、元のクエリで検索する."""
        llm = MagicMock()
        answer_response = MagicMock()
        answer_response.content = "回答"
        llm.ainvoke = AsyncMock(side_effect=[Exception("LLM error"), answer_response])

        vr = _make_vector_result()
        mock_vector_store.search = AsyncMock(return_value=[vr])

        agent = KnowledgeSearchAgent(llm=llm, vector_store=mock_vector_store)
        result = await agent.search_and_answer("CPUが高い")

        # Translation failed, so search is called with the original query only
        mock_vector_store.search.assert_called_once_with("CPUが高い", top_k=5)
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_fallback_search_on_empty_results(self, mock_vector_store):
        """英語変換+元クエリで0件→元クエリのみで再検索."""
        llm = MagicMock()
        translate_response = MagicMock()
        translate_response.content = "cpu high"
        answer_response = MagicMock()
        answer_response.content = "回答"
        llm.ainvoke = AsyncMock(side_effect=[translate_response, answer_response])

        vr = _make_vector_result()
        # First search (combined) returns empty, second (original) returns results
        mock_vector_store.search = AsyncMock(side_effect=[[], [vr]])
        mock_vector_store.count = AsyncMock(return_value=5)

        agent = KnowledgeSearchAgent(llm=llm, vector_store=mock_vector_store)
        result = await agent.search_and_answer("CPUが高い")

        assert mock_vector_store.search.call_count == 2
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_no_vector_store(self):
        """vector_store が None の場合."""
        llm = MagicMock()
        agent = KnowledgeSearchAgent(llm=llm, vector_store=None)
        result = await agent.search_and_answer("test")
        assert "初期化されていません" in result.answer
