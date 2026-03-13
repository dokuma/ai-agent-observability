"""Qdrant ベクトルストア操作の抽象化レイヤー."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError
from qdrant_client import QdrantClient
from qdrant_client.models import Direction, Distance, Filter, OrderBy, PointStruct, VectorParams

logger = logging.getLogger(__name__)


def _log_connection_error(e: APIConnectionError, operation: str) -> None:
    """APIConnectionError の原因チェーンを辿り、HTTP ステータス情報を含めてログ出力."""
    cause = e.__cause__
    # __cause__ チェーンを辿って httpx.HTTPStatusError を探す
    while cause is not None:
        if isinstance(cause, httpx.HTTPStatusError):
            logger.error(
                "Embedding API connection error during %s (underlying HTTP %d): %s — response: %s",
                operation,
                cause.response.status_code,
                e.message,
                cause.response.text[:500],
            )
            return
        cause = getattr(cause, "__cause__", None)
    # HTTP ステータス情報が見つからない場合は __cause__ の型も出力
    cause_info = f"{type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else "no cause"
    logger.error(
        "Embedding API connection error during %s: %s (cause: %s)",
        operation,
        e.message,
        cause_info,
    )


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

    @staticmethod
    def _to_point_id(doc_id: str) -> str:
        """doc_id を Qdrant 互換の UUID 文字列に変換.

        Qdrant はポイント ID として UUID または符号なし整数のみ受け付ける。
        UUID v5 を使用することで同じ doc_id に対して常に同じ UUID が生成される。
        """
        try:
            # 既に有効な UUID ならそのまま使用
            return str(uuid.UUID(doc_id))
        except ValueError:
            return str(uuid.uuid5(uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"), doc_id))

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
        try:
            vector = await self._embeddings.aembed_query(text)
        except APIStatusError as e:
            logger.error(
                "Embedding API error during upsert (doc_id=%s, status=%d): %s",
                doc_id,
                e.status_code,
                e.message,
            )
            raise
        except APIConnectionError as e:
            _log_connection_error(e, "upsert")
            raise
        point_id = self._to_point_id(doc_id)

        def _upsert() -> None:
            self._client.upsert(
                collection_name=self._collection_name,
                points=[
                    PointStruct(
                        id=point_id,
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
        try:
            vectors = await self._embeddings.aembed_documents(texts)
        except APIStatusError as e:
            logger.error(
                "Embedding API error during batch upsert (count=%d, status=%d): %s",
                len(texts),
                e.status_code,
                e.message,
            )
            raise
        except APIConnectionError as e:
            _log_connection_error(e, "batch upsert")
            raise

        def _upsert() -> None:
            points = [
                PointStruct(id=self._to_point_id(doc_id), vector=vec, payload=meta)
                for (doc_id, _, meta), vec in zip(items, vectors, strict=True)
            ]
            self._client.upsert(
                collection_name=self._collection_name,
                points=points,
            )

        await asyncio.to_thread(_upsert)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        query_filter: Filter | None = None,
    ) -> list[VectorSearchResult]:
        """クエリテキストでベクトル検索.

        Args:
            query: 検索クエリテキスト
            top_k: 返却する最大件数
            query_filter: Qdrant Filter（ペイロードフィルタ条件）
        """
        try:
            vector = await self._embeddings.aembed_query(query)
        except APIStatusError as e:
            logger.error(
                "Embedding API error during search (status=%d): %s",
                e.status_code,
                e.message,
            )
            raise
        except APIConnectionError as e:
            _log_connection_error(e, "search")
            raise

        def _search() -> list[VectorSearchResult]:
            hits = self._client.query_points(
                collection_name=self._collection_name,
                query=vector,
                limit=top_k,
                query_filter=query_filter,
            ).points
            return [
                VectorSearchResult(
                    # payload に report_id があればそれを使用（RRF 統合で BM25 側の ID と一致させる）
                    doc_id=(hit.payload or {}).get("report_id", str(hit.id)),
                    score=hit.score,
                    payload=hit.payload or {},
                )
                for hit in hits
            ]

        return await asyncio.to_thread(_search)

    async def retrieve(self, doc_id: str) -> VectorSearchResult | None:
        """ポイントIDで1件取得."""
        point_id = self._to_point_id(doc_id)

        def _retrieve() -> VectorSearchResult | None:
            try:
                points = self._client.retrieve(
                    collection_name=self._collection_name,
                    ids=[point_id],
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                logger.warning("Failed to retrieve point %s", doc_id, exc_info=True)
                return None
            if not points:
                return None
            p = points[0]
            return VectorSearchResult(
                doc_id=(p.payload or {}).get("report_id", str(p.id)),
                score=0.0,
                payload=p.payload or {},
            )

        return await asyncio.to_thread(_retrieve)

    async def scroll(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[VectorSearchResult], int]:
        """ページング付き一覧取得（created_at_ts 降順）."""

        def _scroll() -> tuple[list[VectorSearchResult], int]:
            total_info = self._client.get_collection(self._collection_name)
            total = total_info.points_count or 0

            points, _ = self._client.scroll(
                collection_name=self._collection_name,
                limit=limit + offset,
                with_payload=True,
                with_vectors=False,
                order_by=OrderBy(key="created_at_ts", direction=Direction.DESC),
            )
            # offset 適用
            paged = points[offset : offset + limit]
            results = [
                VectorSearchResult(
                    doc_id=(p.payload or {}).get("report_id", str(p.id)),
                    score=0.0,
                    payload=p.payload or {},
                )
                for p in paged
            ]
            return results, total

        return await asyncio.to_thread(_scroll)

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
