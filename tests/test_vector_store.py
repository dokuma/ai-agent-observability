"""VectorStore のテスト."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_agent_monitoring.core.vector_store import VectorSearchResult, VectorStore


class TestToPointId:
    def test_converts_hex_string_to_uuid(self):
        point_id = VectorStore._to_point_id("bf7e39fd513e")
        # UUID 形式であることを確認
        import uuid

        uuid.UUID(point_id)  # 不正な場合は ValueError

    def test_same_input_produces_same_uuid(self):
        assert VectorStore._to_point_id("abc123") == VectorStore._to_point_id("abc123")

    def test_different_inputs_produce_different_uuids(self):
        assert VectorStore._to_point_id("doc-1") != VectorStore._to_point_id("doc-2")

    def test_valid_uuid_passes_through(self):
        original = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert VectorStore._to_point_id(original) == original


@pytest.fixture
def mock_embeddings():
    emb = MagicMock()
    emb.aembed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    emb.aembed_documents = AsyncMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    return emb


@pytest.fixture
def mock_qdrant_client():
    with patch("ai_agent_monitoring.core.vector_store.QdrantClient") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        yield client


@pytest.fixture
def store(mock_qdrant_client, mock_embeddings):
    return VectorStore(
        qdrant_url="http://localhost:6333",
        collection_name="test_collection",
        embeddings=mock_embeddings,
        vector_size=3,
    )


class TestVectorStoreEnsureCollection:
    @pytest.mark.asyncio
    async def test_creates_collection_when_missing(self, store, mock_qdrant_client):
        collection_info = MagicMock()
        collection_info.name = "other_collection"
        mock_qdrant_client.get_collections.return_value.collections = [collection_info]

        await store.ensure_collection()

        mock_qdrant_client.create_collection.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_existing_collection(self, store, mock_qdrant_client):
        collection_info = MagicMock()
        collection_info.name = "test_collection"
        mock_qdrant_client.get_collections.return_value.collections = [collection_info]

        await store.ensure_collection()

        mock_qdrant_client.create_collection.assert_not_called()


class TestVectorStoreUpsert:
    @pytest.mark.asyncio
    async def test_upsert_calls_embed_and_store(self, store, mock_qdrant_client, mock_embeddings):
        await store.upsert("doc-1", "test text", {"key": "value"})

        mock_embeddings.aembed_query.assert_called_once_with("test text")
        mock_qdrant_client.upsert.assert_called_once()
        args = mock_qdrant_client.upsert.call_args
        assert args.kwargs["collection_name"] == "test_collection"
        points = args.kwargs["points"]
        assert len(points) == 1
        assert points[0].id == VectorStore._to_point_id("doc-1")
        assert points[0].vector == [0.1, 0.2, 0.3]
        assert points[0].payload == {"key": "value"}

    @pytest.mark.asyncio
    async def test_upsert_batch(self, store, mock_qdrant_client, mock_embeddings):
        items = [
            ("doc-1", "text1", {"k": "v1"}),
            ("doc-2", "text2", {"k": "v2"}),
        ]
        await store.upsert_batch(items)

        mock_embeddings.aembed_documents.assert_called_once_with(["text1", "text2"])
        mock_qdrant_client.upsert.assert_called_once()
        points = mock_qdrant_client.upsert.call_args.kwargs["points"]
        assert len(points) == 2

    @pytest.mark.asyncio
    async def test_upsert_batch_empty(self, store, mock_qdrant_client, mock_embeddings):
        await store.upsert_batch([])
        mock_embeddings.aembed_documents.assert_not_called()
        mock_qdrant_client.upsert.assert_not_called()


class TestVectorStoreSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, store, mock_qdrant_client, mock_embeddings):
        hit = MagicMock()
        hit.id = "doc-1"
        hit.score = 0.95
        hit.payload = {"report_id": "r1"}
        mock_qdrant_client.query_points.return_value.points = [hit]

        results = await store.search("test query", top_k=3)

        mock_embeddings.aembed_query.assert_called_once_with("test query")
        assert len(results) == 1
        assert isinstance(results[0], VectorSearchResult)
        assert results[0].doc_id == "doc-1"
        assert results[0].score == 0.95
        assert results[0].payload == {"report_id": "r1"}


class TestVectorStoreHealth:
    @pytest.mark.asyncio
    async def test_health_check_ok(self, store, mock_qdrant_client):
        assert await store.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, store, mock_qdrant_client):
        mock_qdrant_client.get_collections.side_effect = Exception("connection error")
        assert await store.health_check() is False


class TestVectorStoreCount:
    @pytest.mark.asyncio
    async def test_count(self, store, mock_qdrant_client):
        mock_qdrant_client.get_collection.return_value.points_count = 42
        assert await store.count() == 42
