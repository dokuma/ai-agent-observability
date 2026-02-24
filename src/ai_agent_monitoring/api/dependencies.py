"""API 依存注入 — アプリケーション全体の共有リソース管理."""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ai_agent_monitoring.agents.orchestrator import OrchestratorAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import RCAReport
from ai_agent_monitoring.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


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


@dataclass
class InvestigationRecord:
    """調査の実行記録."""

    investigation_id: str
    status: str  # "running" | "completed" | "failed"
    trigger_type: str
    iteration_count: int = 0
    current_stage: str = ""  # 現在のステージ（例: "環境発見中", "メトリクス調査中"）
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error: str = ""
    rca_report: RCAReport | None = None
    mcp_status: dict[str, bool] = field(default_factory=dict)


class AppState:
    """アプリケーション全体の共有ステート.

    FastAPI lifespan で初期化し、Depends で各ルーターに注入する。
    """

    def __init__(self) -> None:
        self.settings = Settings()
        self.registry: ToolRegistry | None = None
        self.orchestrator: OrchestratorAgent | None = None
        self.investigations: dict[str, InvestigationRecord] = {}

    async def initialize(self) -> None:
        """アプリケーション起動時の初期化."""
        # MCP クライアント
        self.registry = ToolRegistry.from_settings(self.settings)
        health = await self.registry.health_check()
        logger.info("MCP health check: %s", health)

        # LLM
        # カスタムヘッダーは event hook で上書き適用する。
        # ChatOpenAI の default_headers に渡すと既存ヘッダー（content-type 等）
        # に追記されてフォーマットが壊れるため、event hook で明示的に上書きする。
        custom_headers = self.settings.llm_custom_headers
        verify_ssl = self.settings.llm_verify_ssl
        is_debug = os.environ.get("OPENAI_LOG", "").lower() == "debug"

        request_hooks_sync: list[object] = []
        request_hooks_async: list[object] = []
        response_hooks_sync: list[object] = []
        response_hooks_async: list[object] = []

        if custom_headers:
            def _apply_custom_headers(request: httpx.Request) -> None:
                for key, value in custom_headers.items():
                    request.headers[key] = value

            async def _apply_custom_headers_async(request: httpx.Request) -> None:
                for key, value in custom_headers.items():
                    request.headers[key] = value

            request_hooks_sync.append(_apply_custom_headers)
            request_hooks_async.append(_apply_custom_headers_async)

        if is_debug:
            request_hooks_sync.append(_log_llm_request)
            request_hooks_async.append(_log_llm_request_async)
            response_hooks_sync.append(_log_llm_response)
            response_hooks_async.append(_log_llm_response_async)

        need_custom_client = custom_headers or not verify_ssl or is_debug
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
        if need_custom_client:
            http_client = httpx.Client(**http_client_kwargs)  # type: ignore[arg-type]
            http_async_client = httpx.AsyncClient(**http_async_client_kwargs)  # type: ignore[arg-type]
        else:
            http_client = None
            http_async_client = None
        llm = ChatOpenAI(
            base_url=self.settings.llm_endpoint,
            model=self.settings.llm_model,
            api_key=SecretStr(self.settings.llm_api_key),
            http_client=http_client,
            http_async_client=http_async_client,
        )

        # デバッグ: 内部クライアントチェーンを検証
        if http_async_client is not None:
            root = getattr(llm, "root_async_client", None)
            internal = getattr(root, "_client", None) if root else None
            logger.info(
                "LLM async client chain: http_async_client=%s, root_async_client=%s, root._client=%s, is_our_client=%s",
                type(http_async_client).__name__,
                type(root).__name__ if root else None,
                type(internal).__name__ if internal else None,
                internal is http_async_client,
            )

        # Orchestrator（registryを渡してhealthy状態を考慮）
        self.orchestrator = OrchestratorAgent(
            llm=llm,
            registry=self.registry,
            settings=self.settings,
            stage_update_callback=self.update_investigation_stage,
        )
        logger.info("Orchestrator Agent initialized")

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

    def complete_investigation(self, inv_id: str, rca_report: RCAReport | None = None) -> None:
        """調査を完了としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.rca_report = rca_report

    def fail_investigation(self, inv_id: str, error: str) -> None:
        """調査を失敗としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "failed"
            record.completed_at = datetime.now()
            record.error = error

    def update_investigation_stage(self, inv_id: str, stage: str, iteration_count: int | None = None) -> None:
        """調査の現在ステージを更新."""
        record = self.investigations.get(inv_id)
        if record:
            record.current_stage = stage
            if iteration_count is not None:
                record.iteration_count = iteration_count
            logger.debug("Investigation %s: stage=%s", inv_id, stage)


# シングルトンインスタンス
app_state = AppState()
