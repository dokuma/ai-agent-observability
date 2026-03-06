"""Qdrant ベクトルストア操作の抽象化レイヤー."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger(__name__)


@dataclass
class VectorSearchResult:
    """ベクトル検索の個別結果."""

    doc_id: str
    score: float
    payload: dict[str, Any]


class VectorStore:
    """Qdrant によるベクトル検索ストア."""

    def __init__(
        self,
        qdrant_url: str,
        collection_name: str,
        embeddings: Any,
        vector_size: int,
    ) -> None:
        self._client = QdrantClient(url=qdrant_url)
        self._collection_name = collection_name
        self._embeddings = embeddings
        self._vector_size = vector_size

    async def ensure_collection(self) -> None:
        """コレクションが存在しなければ作成."""

        def _ensure() -> None:
            collections = self._client.get_collections().collections
            names = [c.name for c in collections]
            if self._collection_name not in names:
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=VectorParams(
                        size=self._vector_size,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection: %s", self._collection_name)

        await asyncio.to_thread(_ensure)

    async def upsert(self, doc_id: str, text: str, metadata: dict[str, Any]) -> None:
        """テキストを embedding してポイントを upsert."""
        vector = await asyncio.to_thread(self._embeddings.embed_query, text)

        def _upsert() -> None:
            self._client.upsert(
                collection_name=self._collection_name,
                points=[
                    PointStruct(
                        id=doc_id,
                        vector=vector,
                        payload=metadata,
                    )
                ],
            )

        await asyncio.to_thread(_upsert)

    async def upsert_batch(self, items: list[tuple[str, str, dict[str, Any]]]) -> None:
        """複数テキストをバッチで embedding + upsert.

        items: list of (doc_id, text, metadata)
        """
        if not items:
            return
        texts = [text for _, text, _ in items]
        vectors = await asyncio.to_thread(self._embeddings.embed_documents, texts)

        def _upsert() -> None:
            points = [
                PointStruct(id=doc_id, vector=vec, payload=meta)
                for (doc_id, _, meta), vec in zip(items, vectors, strict=True)
            ]
            self._client.upsert(
                collection_name=self._collection_name,
                points=points,
            )

        await asyncio.to_thread(_upsert)

    async def search(self, query: str, top_k: int = 5) -> list[VectorSearchResult]:
        """クエリテキストでベクトル検索."""
        vector = await asyncio.to_thread(self._embeddings.embed_query, query)

        def _search() -> list[VectorSearchResult]:
            hits = self._client.query_points(
                collection_name=self._collection_name,
                query=vector,
                limit=top_k,
            ).points
            return [
                VectorSearchResult(
                    doc_id=str(hit.id),
                    score=hit.score,
                    payload=hit.payload or {},
                )
                for hit in hits
            ]

        return await asyncio.to_thread(_search)

    async def count(self) -> int:
        """コレクション内のポイント数を取得."""

        def _count() -> int:
            info = self._client.get_collection(self._collection_name)
            return info.points_count or 0

        return await asyncio.to_thread(_count)

    async def health_check(self) -> bool:
        """Qdrant サーバーのヘルスチェック."""
        try:

            def _check() -> bool:
                self._client.get_collections()
                return True

            return await asyncio.to_thread(_check)
        except Exception:
            logger.warning("Qdrant health check failed", exc_info=True)
            return False
