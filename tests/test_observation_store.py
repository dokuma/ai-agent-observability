"""ObservationStore のテスト."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_agent_monitoring.core.models import (
    KubernetesResourceState,
    KubernetesResult,
    LogsResult,
    MetricsResult,
)
from ai_agent_monitoring.core.observation_store import (
    DEFAULT_HALF_LIFE_DAYS,
    ObservationSearchResult,
    ObservationStore,
    time_decay,
)
from ai_agent_monitoring.core.vector_store import VectorSearchResult


class TestTimeDecay:
    def test_zero_age_returns_one(self):
        now_ts = datetime.now(UTC).timestamp()
        assert time_decay(now_ts) == pytest.approx(1.0, abs=0.01)

    def test_half_life_returns_half(self):
        past_ts = (datetime.now(UTC) - timedelta(days=DEFAULT_HALF_LIFE_DAYS)).timestamp()
        assert time_decay(past_ts) == pytest.approx(0.5, abs=0.02)

    def test_double_half_life_returns_quarter(self):
        past_ts = (datetime.now(UTC) - timedelta(days=DEFAULT_HALF_LIFE_DAYS * 2)).timestamp()
        assert time_decay(past_ts) == pytest.approx(0.25, abs=0.02)

    def test_future_timestamp_returns_one(self):
        future_ts = (datetime.now(UTC) + timedelta(days=1)).timestamp()
        assert time_decay(future_ts) == 1.0

    def test_very_old_approaches_zero(self):
        old_ts = (datetime.now(UTC) - timedelta(days=365)).timestamp()
        assert time_decay(old_ts) < 0.001

    def test_custom_half_life(self):
        past_ts = (datetime.now(UTC) - timedelta(days=7)).timestamp()
        result = time_decay(past_ts, half_life_days=7.0)
        assert result == pytest.approx(0.5, abs=0.02)


@pytest.fixture
def mock_vector_store():
    store = MagicMock()
    store.upsert_batch = AsyncMock()
    store.search = AsyncMock(return_value=[])
    store.ensure_collection = AsyncMock()
    store.count = AsyncMock(return_value=0)
    return store


@pytest.fixture
def observation_store(mock_vector_store):
    return ObservationStore(vector_store=mock_vector_store)


class TestSaveObservations:
    @pytest.mark.asyncio
    async def test_saves_metrics_results(self, observation_store, mock_vector_store):
        metrics = [
            MetricsResult(
                query='rate(cpu_usage{namespace="prod"}[5m])',
                summary="CPU usage exceeded 90%",
                anomalies=["spike at 14:00"],
            ),
        ]
        count = await observation_store.save_observations(
            investigation_id="inv-001",
            metrics_results=metrics,
        )
        assert count == 1
        mock_vector_store.upsert_batch.assert_called_once()
        items = mock_vector_store.upsert_batch.call_args[0][0]
        assert len(items) == 1
        doc_id, text, meta = items[0]
        assert doc_id == "inv-001-metrics-0"
        assert meta["observation_type"] == "metrics"
        assert meta["investigation_id"] == "inv-001"
        assert meta["namespace"] == "prod"
        assert "created_at_ts" in meta
        assert "CPU usage exceeded 90%" in text

    @pytest.mark.asyncio
    async def test_saves_logs_results(self, observation_store, mock_vector_store):
        logs = [
            LogsResult(
                query='{namespace="staging"} |= "error"',
                summary="OOMKilled errors detected",
                error_patterns=["OOMKilled"],
            ),
        ]
        count = await observation_store.save_observations(
            investigation_id="inv-002",
            logs_results=logs,
        )
        assert count == 1
        items = mock_vector_store.upsert_batch.call_args[0][0]
        _, _, meta = items[0]
        assert meta["observation_type"] == "logs"
        assert meta["namespace"] == "staging"

    @pytest.mark.asyncio
    async def test_saves_k8s_results(self, observation_store, mock_vector_store):
        k8s = [
            KubernetesResult(
                summary="Pod crash loop detected",
                anomalies=["CrashLoopBackOff"],
                resource_states=[
                    KubernetesResourceState(
                        kind="Pod",
                        name="api-server-abc",
                        namespace="production",
                        status="CrashLoopBackOff",
                    )
                ],
            ),
        ]
        count = await observation_store.save_observations(
            investigation_id="inv-003",
            k8s_results=k8s,
        )
        assert count == 1
        items = mock_vector_store.upsert_batch.call_args[0][0]
        _, text, meta = items[0]
        assert meta["observation_type"] == "k8s"
        assert meta["namespace"] == "production"
        assert "CrashLoopBackOff" in text

    @pytest.mark.asyncio
    async def test_saves_all_types(self, observation_store, mock_vector_store):
        count = await observation_store.save_observations(
            investigation_id="inv-004",
            metrics_results=[MetricsResult(query="up", summary="All targets up")],
            logs_results=[LogsResult(query="{app='web'}", summary="No errors")],
            k8s_results=[KubernetesResult(summary="All pods running")],
        )
        assert count == 3
        items = mock_vector_store.upsert_batch.call_args[0][0]
        types = [item[2]["observation_type"] for item in items]
        assert types == ["metrics", "logs", "k8s"]

    @pytest.mark.asyncio
    async def test_skips_empty_summaries(self, observation_store, mock_vector_store):
        count = await observation_store.save_observations(
            investigation_id="inv-005",
            metrics_results=[MetricsResult(query="up", summary="")],
        )
        assert count == 0
        mock_vector_store.upsert_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_upsert_failure(self, observation_store, mock_vector_store):
        mock_vector_store.upsert_batch.side_effect = Exception("connection error")
        count = await observation_store.save_observations(
            investigation_id="inv-006",
            metrics_results=[MetricsResult(query="up", summary="test")],
        )
        assert count == 0


class TestSearchSimilar:
    @pytest.mark.asyncio
    async def test_returns_rescored_results(self, observation_store, mock_vector_store):
        now_ts = datetime.now(UTC).timestamp()
        mock_vector_store.search = AsyncMock(
            return_value=[
                VectorSearchResult(
                    doc_id="inv-001-metrics-0",
                    score=0.9,
                    payload={
                        "observation_type": "metrics",
                        "summary": "CPU high",
                        "investigation_id": "inv-001",
                        "created_at_ts": now_ts,
                    },
                ),
            ]
        )
        results = await observation_store.search_similar("CPU usage high")
        assert len(results) == 1
        assert isinstance(results[0], ObservationSearchResult)
        assert results[0].raw_score == 0.9
        # time_decay for very recent should be ~1.0
        assert results[0].score == pytest.approx(0.9, abs=0.05)
        assert results[0].observation_type == "metrics"

    @pytest.mark.asyncio
    async def test_old_results_have_lower_score(self, observation_store, mock_vector_store):
        now_ts = datetime.now(UTC).timestamp()
        old_ts = (datetime.now(UTC) - timedelta(days=28)).timestamp()
        mock_vector_store.search = AsyncMock(
            return_value=[
                VectorSearchResult(
                    doc_id="recent",
                    score=0.8,
                    payload={
                        "observation_type": "metrics",
                        "summary": "recent",
                        "investigation_id": "inv-1",
                        "created_at_ts": now_ts,
                    },
                ),
                VectorSearchResult(
                    doc_id="old",
                    score=0.8,
                    payload={
                        "observation_type": "metrics",
                        "summary": "old",
                        "investigation_id": "inv-2",
                        "created_at_ts": old_ts,
                    },
                ),
            ]
        )
        results = await observation_store.search_similar("test", top_k=2)
        assert len(results) == 2
        # Recent should score higher than old
        assert results[0].doc_id == "recent"
        assert results[0].score > results[1].score

    @pytest.mark.asyncio
    async def test_filter_by_observation_type(self, observation_store, mock_vector_store):
        await observation_store.search_similar("test", observation_type="logs")
        call_kwargs = mock_vector_store.search.call_args[1]
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert len(query_filter.must) == 1
        assert query_filter.must[0].key == "observation_type"

    @pytest.mark.asyncio
    async def test_no_filter_when_type_is_none(self, observation_store, mock_vector_store):
        await observation_store.search_similar("test")
        call_kwargs = mock_vector_store.search.call_args[1]
        assert call_kwargs["query_filter"] is None

    @pytest.mark.asyncio
    async def test_filter_by_namespace(self, observation_store, mock_vector_store):
        """target_namespaces 指定時は namespace フィルタが適用される."""
        await observation_store.search_similar("test", target_namespaces=["prod"])
        call_kwargs = mock_vector_store.search.call_args[1]
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        # namespace フィルタがある
        ns_condition = next(c for c in query_filter.must if c.key == "namespace")
        # prod と "" (namespace 未設定) が含まれる
        assert "prod" in ns_condition.match.any
        assert "" in ns_condition.match.any

    @pytest.mark.asyncio
    async def test_filter_by_multiple_namespaces(self, observation_store, mock_vector_store):
        """複数 namespace 指定時はすべてがフィルタに含まれる."""
        await observation_store.search_similar("test", target_namespaces=["prod", "staging"])
        call_kwargs = mock_vector_store.search.call_args[1]
        query_filter = call_kwargs["query_filter"]
        ns_condition = next(c for c in query_filter.must if c.key == "namespace")
        assert "prod" in ns_condition.match.any
        assert "staging" in ns_condition.match.any
        assert "" in ns_condition.match.any

    @pytest.mark.asyncio
    async def test_no_namespace_filter_when_none(self, observation_store, mock_vector_store):
        """target_namespaces が None の場合はフィルタなし."""
        await observation_store.search_similar("test", target_namespaces=None)
        call_kwargs = mock_vector_store.search.call_args[1]
        assert call_kwargs["query_filter"] is None

    @pytest.mark.asyncio
    async def test_combined_type_and_namespace_filter(self, observation_store, mock_vector_store):
        """observation_type と target_namespaces の同時指定で両方のフィルタが適用される."""
        await observation_store.search_similar("test", observation_type="metrics", target_namespaces=["prod"])
        call_kwargs = mock_vector_store.search.call_args[1]
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert len(query_filter.must) == 2
        keys = {c.key for c in query_filter.must}
        assert keys == {"observation_type", "namespace"}

    @pytest.mark.asyncio
    async def test_handles_search_failure(self, observation_store, mock_vector_store):
        mock_vector_store.search = AsyncMock(side_effect=Exception("search error"))
        results = await observation_store.search_similar("test")
        assert results == []


class TestExtractNamespace:
    def test_extracts_from_promql(self):
        assert ObservationStore._extract_namespace('rate(cpu{namespace="prod"}[5m])') == "prod"

    def test_extracts_from_logql(self):
        assert ObservationStore._extract_namespace('{namespace="staging"} |= "err"') == "staging"

    def test_extracts_regex_match(self):
        assert ObservationStore._extract_namespace('{namespace=~"prod.*"}') == "prod.*"

    def test_returns_empty_for_no_namespace(self):
        assert ObservationStore._extract_namespace("rate(cpu[5m])") == ""

    def test_returns_empty_for_empty_query(self):
        assert ObservationStore._extract_namespace("") == ""


class TestBuildText:
    def test_build_metrics_text(self):
        mr = MetricsResult(
            query="rate(cpu[5m])",
            summary="CPU high",
            anomalies=["spike"],
        )
        text = ObservationStore._build_metrics_text(mr)
        assert "CPU high" in text
        assert "rate(cpu[5m])" in text
        assert "spike" in text

    def test_build_logs_text(self):
        lr = LogsResult(
            query='{app="web"}',
            summary="Errors found",
            error_patterns=["OOM", "timeout"],
        )
        text = ObservationStore._build_logs_text(lr)
        assert "Errors found" in text
        assert "OOM" in text

    def test_build_k8s_text(self):
        kr = KubernetesResult(
            summary="Pod issues",
            anomalies=["CrashLoop"],
            events=["BackOff"],
            resource_states=[
                KubernetesResourceState(
                    kind="Pod",
                    name="web-1",
                    namespace="prod",
                    status="CrashLoopBackOff",
                )
            ],
        )
        text = ObservationStore._build_k8s_text(kr)
        assert "Pod issues" in text
        assert "CrashLoop" in text
        assert "Pod/web-1" in text
