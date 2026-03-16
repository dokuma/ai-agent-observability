"""Observation Store — 各エージェントの調査結果をベクトル保存し後続調査で活用."""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from ai_agent_monitoring.core.models import KubernetesResult, LogsResult, MetricsResult
from ai_agent_monitoring.core.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 半減期（日）: 14日で類似度スコアが半分になる
DEFAULT_HALF_LIFE_DAYS = 14.0


def time_decay(created_at_ts: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """半減期ベースの指数減衰.

    Args:
        created_at_ts: 観測の Unix timestamp
        half_life_days: 半減期（日数）

    Returns:
        0.0〜1.0 の減衰係数
    """
    now_ts = datetime.now(UTC).timestamp()
    age_days = (now_ts - created_at_ts) / 86400.0
    if age_days <= 0:
        return 1.0
    return math.exp(-0.693 * age_days / half_life_days)


@dataclass
class ObservationSearchResult:
    """観測データの検索結果."""

    doc_id: str
    observation_type: str  # "metrics" | "logs" | "k8s"
    summary: str
    score: float  # time_decay 適用後のスコア
    raw_score: float  # ベクトル類似度（decay 前）
    investigation_id: str
    created_at_ts: float


class ObservationStore:
    """各エージェントの調査結果を Qdrant に保存・検索するストア."""

    def __init__(
        self,
        vector_store: VectorStore,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ) -> None:
        self._vector_store = vector_store
        self._half_life_days = half_life_days

    async def ensure_collection(self) -> None:
        """コレクションが存在しなければ作成."""
        await self._vector_store.ensure_collection()

    async def save_observations(
        self,
        investigation_id: str,
        metrics_results: list[MetricsResult] | None = None,
        logs_results: list[LogsResult] | None = None,
        k8s_results: list[KubernetesResult] | None = None,
    ) -> int:
        """各エージェントの結果を一括保存.

        Returns:
            保存した観測データの件数
        """
        now_ts = datetime.now(UTC).timestamp()
        items: list[tuple[str, str, dict[str, Any]]] = []

        for i, mr in enumerate(metrics_results or []):
            if not mr.summary:
                continue
            doc_id = f"{investigation_id}-metrics-{i}"
            text = self._build_metrics_text(mr)
            metadata: dict[str, Any] = {
                "observation_type": "metrics",
                "investigation_id": investigation_id,
                "namespace": self._extract_namespace(mr.query),
                "created_at_ts": now_ts,
                "summary": mr.summary[:500],
                "raw_tool_outputs": mr.tool_outputs,
            }
            items.append((doc_id, text, metadata))

        for i, lr in enumerate(logs_results or []):
            if not lr.summary:
                continue
            doc_id = f"{investigation_id}-logs-{i}"
            text = self._build_logs_text(lr)
            metadata = {
                "observation_type": "logs",
                "investigation_id": investigation_id,
                "namespace": self._extract_namespace(lr.query),
                "created_at_ts": now_ts,
                "summary": lr.summary[:500],
                "raw_tool_outputs": lr.tool_outputs,
            }
            items.append((doc_id, text, metadata))

        for i, kr in enumerate(k8s_results or []):
            if not kr.summary:
                continue
            doc_id = f"{investigation_id}-k8s-{i}"
            text = self._build_k8s_text(kr)
            # K8s の namespace はリソース状態から取得
            ns = ""
            if kr.resource_states:
                ns = kr.resource_states[0].namespace
            metadata = {
                "observation_type": "k8s",
                "investigation_id": investigation_id,
                "namespace": ns,
                "created_at_ts": now_ts,
                "summary": kr.summary[:500],
                "raw_tool_outputs": kr.tool_outputs,
            }
            items.append((doc_id, text, metadata))

        if not items:
            logger.debug("No observations to save for investigation %s", investigation_id)
            return 0

        try:
            await self._vector_store.upsert_batch(items)
            logger.info(
                "Saved %d observations for investigation %s",
                len(items),
                investigation_id,
            )
        except Exception:
            logger.warning(
                "Failed to save observations for investigation %s",
                investigation_id,
                exc_info=True,
            )
            return 0

        return len(items)

    async def search_similar(
        self,
        query: str,
        top_k: int = 5,
        observation_type: str | None = None,
        target_namespaces: list[str] | None = None,
    ) -> list[ObservationSearchResult]:
        """類似の過去観測を検索し、time_decay でリスコアリング.

        Args:
            query: 検索クエリテキスト
            top_k: 返却する最大件数
            observation_type: フィルタ（"metrics" | "logs" | "k8s"）。None で全種別。
            target_namespaces: namespace フィルタ。指定時は該当 namespace の
                観測のみ返す（namespace 未設定の観測も含む）。
        """
        # フィルタ構築
        must_conditions: list[Any] = []
        if observation_type:
            must_conditions.append(
                FieldCondition(
                    key="observation_type",
                    match=MatchValue(value=observation_type),
                )
            )
        if target_namespaces:
            # 指定 namespace + namespace 未設定（""）の観測を含める
            allowed = [*target_namespaces, ""]
            must_conditions.append(
                FieldCondition(
                    key="namespace",
                    match=MatchAny(any=allowed),
                )
            )
        query_filter: Filter | None = Filter(must=must_conditions) if must_conditions else None

        # 多めに取得してリスコアリング後に絞り込む
        fetch_k = top_k * 3
        try:
            raw_results = await self._vector_store.search(
                query=query,
                top_k=fetch_k,
                query_filter=query_filter,
            )
        except Exception:
            logger.warning("Observation search failed", exc_info=True)
            return []

        # time_decay でリスコアリング
        scored: list[ObservationSearchResult] = []
        for r in raw_results:
            created_at_ts = r.payload.get("created_at_ts", 0.0)
            decay = time_decay(created_at_ts, self._half_life_days)
            final_score = r.score * decay
            scored.append(
                ObservationSearchResult(
                    doc_id=r.doc_id,
                    observation_type=r.payload.get("observation_type", ""),
                    summary=r.payload.get("summary", ""),
                    score=final_score,
                    raw_score=r.score,
                    investigation_id=r.payload.get("investigation_id", ""),
                    created_at_ts=created_at_ts,
                )
            )

        # スコア順にソートして top_k 件返却
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]

    async def count(self) -> int:
        """コレクション内のポイント数を取得."""
        return await self._vector_store.count()

    # ---- テキスト構築 ----

    @staticmethod
    def _build_metrics_text(mr: MetricsResult) -> str:
        """MetricsResult から embedding 用テキストを構築."""
        from ai_agent_monitoring.tools.metrics_preprocessor import summarize_prometheus_result

        parts = [mr.summary]
        if mr.query:
            parts.append(f"Query: {mr.query}")
        if mr.anomalies:
            parts.append("Anomalies: " + "; ".join(mr.anomalies))
        # tool_outputs から時系列統計サマリを抽出
        for output in mr.tool_outputs:
            prom_summary = summarize_prometheus_result(output)
            if prom_summary:
                parts.append(f"統計サマリ:\n{prom_summary}")
        return "\n".join(parts)

    @staticmethod
    def _build_logs_text(lr: LogsResult) -> str:
        """LogsResult から embedding 用テキストを構築."""
        parts = [lr.summary]
        if lr.query:
            parts.append(f"Query: {lr.query}")
        if lr.error_patterns:
            parts.append("Error patterns: " + "; ".join(lr.error_patterns))
        return "\n".join(parts)

    @staticmethod
    def _build_k8s_text(kr: KubernetesResult) -> str:
        """KubernetesResult から embedding 用テキストを構築."""
        parts = [kr.summary]
        if kr.anomalies:
            parts.append("Anomalies: " + "; ".join(kr.anomalies))
        if kr.events:
            parts.append("Events: " + "; ".join(kr.events[:10]))
        # リソース状態の要約
        for rs in kr.resource_states[:5]:
            parts.append(f"{rs.kind}/{rs.name} ({rs.namespace}): {rs.status}")
        return "\n".join(parts)

    @staticmethod
    def _extract_namespace(query: str) -> str:
        """PromQL/LogQL クエリから namespace を抽出（ベストエフォート）."""
        if not query:
            return ""
        # {namespace="xxx"} or {namespace=~"xxx"} パターン
        import re

        m = re.search(r'namespace\s*=~?\s*"([^"]+)"', query)
        return m.group(1) if m else ""
