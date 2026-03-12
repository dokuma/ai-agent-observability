"""API 依存注入 — アプリケーション全体の共有リソース管理."""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from openai import APIConnectionError, APIStatusError
from pydantic import SecretStr

from ai_agent_monitoring.agents.knowledge_search_agent import KnowledgeSearchAgent
from ai_agent_monitoring.agents.orchestrator import OrchestratorAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.datasource import DatasourcePreferenceStore
from ai_agent_monitoring.core.hybrid_search import HybridSearcher
from ai_agent_monitoring.core.llm_retry import RateLimitRetryWrapper
from ai_agent_monitoring.core.models import RCAReport
from ai_agent_monitoring.core.observation_store import ObservationStore
from ai_agent_monitoring.core.report_store import ReportStore
from ai_agent_monitoring.core.vector_store import VectorStore
from ai_agent_monitoring.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _log_emb_connection_error(
    e: APIConnectionError,
    endpoint: str,
    model: str,
    extra: str = "",
) -> None:
    """APIConnectionError の原因チェーンから HTTP ステータス情報を抽出してログ出力."""
    cause = e.__cause__
    while cause is not None:
        if isinstance(cause, httpx.HTTPStatusError):
            logger.error(
                "Embedding API connection error (endpoint=%s, model=%s, underlying HTTP %d): %s — response: %s%s",
                endpoint,
                model,
                cause.response.status_code,
                e.message,
                cause.response.text[:500],
                f" ({extra})" if extra else "",
            )
            return
        cause = getattr(cause, "__cause__", None)
    cause_info = f"{type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else "no cause"
    logger.error(
        "Embedding API connection error (endpoint=%s, model=%s): %s (cause: %s)%s",
        endpoint,
        model,
        e.message,
        cause_info,
        f" ({extra})" if extra else "",
    )


def _log_llm_request(request: httpx.Request) -> None:
    """LLM への HTTP リクエストをログ出力（同期用、OPENAI_LOG=debug 時のみ有効）."""
    logger.info(
        "LLM HTTP Request: %s %s headers=%s body=%s",
        request.method,
        request.url,
        dict(request.headers),
        request.content.decode("utf-8", errors="replace")[:2000],
    )


async def _log_llm_request_async(request: httpx.Request) -> None:
    """LLM への HTTP リクエストをログ出力（非同期用、OPENAI_LOG=debug 時のみ有効）."""
    logger.info(
        "LLM HTTP Request: %s %s headers=%s body=%s",
        request.method,
        request.url,
        dict(request.headers),
        request.content.decode("utf-8", errors="replace")[:2000],
    )


def _log_llm_response(response: httpx.Response) -> None:
    """LLM からの HTTP レスポンスをログ出力（同期用）."""
    response.read()
    logger.info(
        "LLM HTTP Response: status=%s headers=%s body=%s",
        response.status_code,
        dict(response.headers),
        response.text[:2000],
    )


async def _log_llm_response_async(response: httpx.Response) -> None:
    """LLM からの HTTP レスポンスをログ出力（非同期用）."""
    await response.aread()
    logger.info(
        "LLM HTTP Response: status=%s headers=%s body=%s",
        response.status_code,
        dict(response.headers),
        response.text[:2000],
    )


def _build_http_clients(
    custom_headers: dict[str, str],
    verify_ssl: bool,
    is_debug: bool,
) -> tuple[httpx.Client | None, httpx.AsyncClient | None]:
    """カスタムヘッダー・SSL・デバッグログ付き httpx クライアントを構築.

    LLM と Embedding の両方で利用する。呼び出しごとに新しいインスタンスを返す。
    """
    request_hooks_sync: list[object] = []
    request_hooks_async: list[object] = []
    response_hooks_sync: list[object] = []
    response_hooks_async: list[object] = []

    if custom_headers:

        def _apply(request: httpx.Request) -> None:
            for key, value in custom_headers.items():
                request.headers[key] = value

        async def _apply_async(request: httpx.Request) -> None:
            for key, value in custom_headers.items():
                request.headers[key] = value

        request_hooks_sync.append(_apply)
        request_hooks_async.append(_apply_async)

    if is_debug:
        request_hooks_sync.append(_log_llm_request)
        request_hooks_async.append(_log_llm_request_async)
        response_hooks_sync.append(_log_llm_response)
        response_hooks_async.append(_log_llm_response_async)

    need_custom_client = bool(custom_headers) or not verify_ssl or is_debug
    if not need_custom_client:
        return None, None

    http_client_kwargs: dict[str, object] = {"verify": verify_ssl}
    http_async_client_kwargs: dict[str, object] = {"verify": verify_ssl}
    if request_hooks_sync or response_hooks_sync:
        http_client_kwargs["event_hooks"] = {
            "request": request_hooks_sync,
            "response": response_hooks_sync,
        }
        http_async_client_kwargs["event_hooks"] = {
            "request": request_hooks_async,
            "response": response_hooks_async,
        }
    return (
        httpx.Client(**http_client_kwargs),  # type: ignore[arg-type]
        httpx.AsyncClient(**http_async_client_kwargs),  # type: ignore[arg-type]
    )


@dataclass
class InvestigationRecord:
    """調査の実行記録."""

    investigation_id: str
    status: str  # "running" | "completed" | "failed" | "waiting_for_input" | "cancelled"
    trigger_type: str
    iteration_count: int = 0
    current_stage: str = ""  # 現在のステージ（例: "環境発見中", "メトリクス調査中"）
    stage_detail: str = ""  # ステージ内の詳細（例: "ReAct step 2: grafana_query_prometheus を実行中"）
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error: str = ""
    rca_report: RCAReport | None = None
    mcp_status: dict[str, bool] = field(default_factory=dict)
    # interrupt/resume 用
    pending_input: dict[str, Any] | str | None = None
    compiled_graph: Any = None
    graph_config: dict[str, Any] | None = None
    # asyncio.Task 参照（キャンセル用）
    task: Any = None
    # report_search 結果（ポーリングで取得するため保持）
    report_search_answer: str | None = None
    # report_search → 調査自動移行時のフォローアップ調査ID
    followup_investigation_id: str | None = None


class AppState:
    """アプリケーション全体の共有ステート.

    FastAPI lifespan で初期化し、Depends で各ルーターに注入する。
    """

    def __init__(self) -> None:
        self.settings = Settings()
        self.registry: ToolRegistry | None = None
        self.orchestrator: OrchestratorAgent | None = None
        self.investigations: dict[str, InvestigationRecord] = {}
        self.checkpointer = MemorySaver()
        self.ds_preference_store: DatasourcePreferenceStore | None = None
        self.report_store: ReportStore | None = None
        self.knowledge_search_agent: KnowledgeSearchAgent | None = None
        self.vector_store: VectorStore | None = None
        self.observation_store: ObservationStore | None = None
        self.hybrid_searcher: HybridSearcher | None = None

    async def initialize(self) -> None:
        """アプリケーション起動時の初期化."""
        # MCP クライアント
        self.registry = ToolRegistry.from_settings(self.settings)
        health = await self.registry.health_check()
        logger.debug("MCP health check: %s", health)

        # MCP プロトコルレベルのトランスポート自動検出
        # HTTP ヘルスチェック通過後に実際の MCP 接続を試行し、
        # SSE/Streamable HTTP の互換性問題を自動的に解決する
        transports = await self.registry.auto_detect_transports()
        logger.debug("MCP transport detection: %s", transports)

        # LLM
        # カスタムヘッダーは event hook で上書き適用する。
        # ChatOpenAI の default_headers に渡すと既存ヘッダー（content-type 等）
        # に追記されてフォーマットが壊れるため、event hook で明示的に上書きする。
        custom_headers = self.settings.llm_custom_headers
        verify_ssl = self.settings.llm_verify_ssl
        is_debug = os.environ.get("OPENAI_LOG", "").lower() == "debug"

        http_client, http_async_client = _build_http_clients(custom_headers, verify_ssl, is_debug)
        raw_llm = ChatOpenAI(
            base_url=self.settings.llm_endpoint,
            model=self.settings.llm_model,
            api_key=SecretStr(self.settings.llm_api_key),
            max_retries=self.settings.llm_max_retries,
            http_client=http_client,
            http_async_client=http_async_client,
        )
        llm = RateLimitRetryWrapper(
            raw_llm,
            max_attempts=self.settings.llm_rate_limit_max_attempts,
            wait_min=self.settings.llm_rate_limit_wait_min,
            wait_max=self.settings.llm_rate_limit_wait_max,
        )

        # デバッグ: 内部クライアントチェーンを検証
        if http_async_client is not None:
            root = getattr(raw_llm, "root_async_client", None)
            internal = getattr(root, "_client", None) if root else None
            logger.info(
                "LLM async client chain: http_async_client=%s, root_async_client=%s, root._client=%s, is_our_client=%s",
                type(http_async_client).__name__,
                type(root).__name__ if root else None,
                type(internal).__name__ if internal else None,
                internal is http_async_client,
            )

        # データソースプリファレンスストア
        self.ds_preference_store = DatasourcePreferenceStore(Path(self.settings.datasource_preferences_path))

        # レポートストア
        self.report_store = ReportStore(db_path=self.settings.report_store_path)
        self.report_store.initialize()
        logger.info("ReportStore initialized (%d reports)", self.report_store.count())

        # Qdrant ベクトルストア（qdrant_enabled の場合のみ）
        if self.settings.qdrant_enabled:
            await self._init_vector_store()

        # レポート検索エージェント
        self.knowledge_search_agent = KnowledgeSearchAgent(
            llm=llm,
            report_store=self.report_store,
            hybrid_searcher=self.hybrid_searcher,
        )

        # Orchestrator（registryを渡してhealthy状態を考慮）
        self.orchestrator = OrchestratorAgent(
            llm=llm,
            registry=self.registry,
            settings=self.settings,
            stage_update_callback=self.update_investigation_stage,
            ds_preference_store=self.ds_preference_store,
            observation_store=self.observation_store,
        )
        logger.info("Orchestrator Agent initialized")

    async def _init_vector_store(self) -> None:
        """Qdrant + Embedding + HybridSearcher を初期化."""
        from langchain_openai import OpenAIEmbeddings

        emb_endpoint = self.settings.embedding_endpoint or self.settings.llm_endpoint
        emb_api_key = self.settings.embedding_api_key or self.settings.llm_api_key

        # Embedding にも LLM と同じカスタムヘッダー・SSL 設定を適用
        custom_headers = self.settings.llm_custom_headers
        verify_ssl = self.settings.llm_verify_ssl
        is_debug = os.environ.get("OPENAI_LOG", "").lower() == "debug"
        emb_http_client, emb_http_async_client = _build_http_clients(custom_headers, verify_ssl, is_debug)

        emb_kwargs: dict[str, Any] = {
            "base_url": emb_endpoint,
            "model": self.settings.embedding_model,
            "api_key": emb_api_key,
        }
        if self.settings.embedding_dimensions > 0:
            emb_kwargs["dimensions"] = self.settings.embedding_dimensions
        if emb_http_client is not None:
            emb_kwargs["http_client"] = emb_http_client
        if emb_http_async_client is not None:
            emb_kwargs["http_async_client"] = emb_http_async_client

        embeddings = OpenAIEmbeddings(**emb_kwargs)

        # embedding 次元数を取得（dimensions 指定時はそれを使用、未指定時はテスト embed で取得）
        if self.settings.embedding_dimensions > 0:
            vector_size = self.settings.embedding_dimensions
        else:
            logger.info(
                "Detecting embedding dimension: endpoint=%s, model=%s",
                emb_endpoint,
                self.settings.embedding_model,
            )
            try:
                test_vec = await embeddings.aembed_query("dimension test")
                vector_size = len(test_vec)
                logger.info("Detected embedding dimension: %d", vector_size)
            except httpx.ConnectError as e:
                logger.error(
                    "Embedding endpoint unreachable (endpoint=%s): %s",
                    emb_endpoint,
                    e,
                )
                vector_size = 1536
            except httpx.TimeoutException as e:
                logger.error(
                    "Embedding request timed out (endpoint=%s): %s",
                    emb_endpoint,
                    e,
                )
                vector_size = 1536
            except APIStatusError as e:
                logger.error(
                    "Embedding API returned HTTP %d (endpoint=%s, model=%s): %s",
                    e.status_code,
                    emb_endpoint,
                    self.settings.embedding_model,
                    e.message,
                )
                vector_size = 1536
            except APIConnectionError as e:
                _log_emb_connection_error(e, emb_endpoint, self.settings.embedding_model)
                vector_size = 1536
            except httpx.HTTPStatusError as e:
                logger.error(
                    "Embedding API returned HTTP %d (endpoint=%s, model=%s): %s",
                    e.response.status_code,
                    emb_endpoint,
                    self.settings.embedding_model,
                    e.response.text,
                )
                vector_size = 1536
            except Exception as e:
                logger.error(
                    "Unexpected error detecting embedding dimension (endpoint=%s, model=%s, error_type=%s): %s",
                    emb_endpoint,
                    self.settings.embedding_model,
                    type(e).__name__,
                    e,
                )
                vector_size = 1536
            if vector_size == 1536:
                logger.warning(
                    "Using default embedding dimension 1536. Set EMBEDDING_DIMENSIONS to skip auto-detection."
                )

        self.vector_store = VectorStore(
            qdrant_url=self.settings.qdrant_url,
            collection_name=self.settings.qdrant_reports_collection,
            embeddings=embeddings,
            vector_size=vector_size,
        )

        try:
            await self.vector_store.ensure_collection()
            logger.info("Qdrant vector store initialized (collection: %s)", self.settings.qdrant_reports_collection)
        except Exception as e:
            logger.error(
                "Failed to initialize Qdrant collection (url=%s, collection=%s, error_type=%s): %s",
                self.settings.qdrant_url,
                self.settings.qdrant_reports_collection,
                type(e).__name__,
                e,
            )
            self.vector_store = None

        if self.vector_store and self.report_store:
            self.hybrid_searcher = HybridSearcher(
                report_store=self.report_store,
                vector_store=self.vector_store,
                rrf_k=self.settings.rrf_k,
            )
            await self._migrate_reports_to_qdrant()

        # Observations 用 VectorStore（同じ embedding、別コレクション）
        obs_vector_store = VectorStore(
            qdrant_url=self.settings.qdrant_url,
            collection_name=self.settings.qdrant_checkpoints_collection,
            embeddings=embeddings,
            vector_size=vector_size,
        )
        try:
            await obs_vector_store.ensure_collection()
            self.observation_store = ObservationStore(vector_store=obs_vector_store)
            logger.info(
                "ObservationStore initialized (collection: %s)",
                self.settings.qdrant_checkpoints_collection,
            )
        except Exception as e:
            logger.error(
                "Failed to initialize observations collection (error_type=%s): %s",
                type(e).__name__,
                e,
            )

    async def _migrate_reports_to_qdrant(self) -> None:
        """SQLite のレポートを Qdrant に差分マイグレーション."""
        if not self.vector_store or not self.report_store:
            return
        try:
            qdrant_count = await self.vector_store.count()
            sqlite_count = self.report_store.count()
            if qdrant_count >= sqlite_count:
                logger.info("Qdrant already up to date (%d points)", qdrant_count)
                return

            logger.info("Migrating reports to Qdrant: sqlite=%d, qdrant=%d", sqlite_count, qdrant_count)
            reports, _ = self.report_store.list_reports(offset=0, limit=sqlite_count)
            batch: list[tuple[str, str, dict[str, Any]]] = []
            batch_size = 50
            for report in reports:
                text = self.report_store._extract_searchable_text_from_json(report.report.model_dump_json())
                metadata = {
                    "report_id": report.id,
                    "investigation_id": report.investigation_id,
                    "trigger_type": report.report.trigger_type.value,
                    "created_at_ts": report.created_at.timestamp(),
                }
                batch.append((report.id, text, metadata))
                if len(batch) >= batch_size:
                    await self.vector_store.upsert_batch(batch)
                    batch = []
            if batch:
                await self.vector_store.upsert_batch(batch)
            logger.info("Migrated %d reports to Qdrant", len(reports))
        except Exception as e:
            logger.error(
                "Failed to migrate reports to Qdrant (error_type=%s): %s",
                type(e).__name__,
                e,
            )

    async def _upsert_report_vector(self, report_id: str, inv_id: str, rca_report: RCAReport) -> None:
        """レポートを Qdrant にベクトル保存（失敗時はログのみ）."""
        if not self.vector_store or not self.report_store:
            return
        try:
            text = self.report_store._extract_searchable_text_from_json(rca_report.model_dump_json())
            metadata: dict[str, Any] = {
                "report_id": report_id,
                "investigation_id": inv_id,
                "trigger_type": rca_report.trigger_type.value,
                "created_at_ts": datetime.now().timestamp(),
            }
            await self.vector_store.upsert(report_id, text, metadata)
            logger.info("Report %s upserted to Qdrant", report_id)
        except APIStatusError as e:
            logger.error(
                "Failed to upsert report %s: embedding API returned HTTP %d: %s",
                report_id,
                e.status_code,
                e.message,
            )
        except APIConnectionError as e:
            _log_emb_connection_error(
                e,
                self.settings.embedding_endpoint or self.settings.llm_endpoint,
                self.settings.embedding_model,
                extra=f"report_id={report_id}",
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to upsert report %s: embedding API returned HTTP %d: %s",
                report_id,
                e.response.status_code,
                e.response.text,
            )
        except Exception as e:
            logger.error(
                "Failed to upsert report %s to Qdrant (error_type=%s): %s",
                report_id,
                type(e).__name__,
                e,
            )

    async def shutdown(self) -> None:
        """アプリケーション終了時のクリーンアップ."""
        logger.info("Shutting down application")

    def create_investigation(self, trigger_type: str) -> str:
        """新しい調査レコードを作成しIDを返す."""
        inv_id = uuid4().hex[:12]
        self.investigations[inv_id] = InvestigationRecord(
            investigation_id=inv_id,
            status="running",
            trigger_type=trigger_type,
        )
        return inv_id

    def get_investigation(self, inv_id: str) -> InvestigationRecord | None:
        """調査レコードを取得."""
        return self.investigations.get(inv_id)

    async def complete_investigation(self, inv_id: str, rca_report: RCAReport | None = None) -> None:
        """調査を完了としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.rca_report = rca_report
            if rca_report and self.report_store:
                try:
                    report_id = self.report_store.save_report(inv_id, rca_report)
                    logger.info("RCA report saved: %s (investigation: %s)", report_id, inv_id)
                    await self._upsert_report_vector(report_id, inv_id, rca_report)
                except Exception:
                    logger.exception("Failed to save RCA report for investigation %s", inv_id)

    def fail_investigation(self, inv_id: str, error: str) -> None:
        """調査を失敗としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "failed"
            record.completed_at = datetime.now()
            record.error = error

    def set_waiting_for_input(
        self,
        inv_id: str,
        pending_input: dict[str, Any] | str,
        compiled_graph: Any,
        config: dict[str, Any],
    ) -> None:
        """調査をユーザ入力待ちにする."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "waiting_for_input"
            record.pending_input = pending_input
            record.compiled_graph = compiled_graph
            record.graph_config = config
            record.current_stage = "ユーザ入力を待機中"
            input_type = pending_input.get("type") if isinstance(pending_input, dict) else "text"
            logger.info("Investigation %s waiting for input: %s", inv_id, input_type)

    def update_investigation_stage(
        self,
        inv_id: str,
        stage: str,
        iteration_count: int | None = None,
        detail: str = "",
    ) -> None:
        """調査の現在ステージを更新."""
        record = self.investigations.get(inv_id)
        if record:
            record.current_stage = stage
            record.stage_detail = detail
            if iteration_count is not None:
                record.iteration_count = iteration_count
            logger.debug("Investigation %s: stage=%s detail=%s", inv_id, stage, detail)


# シングルトンインスタンス
app_state = AppState()
