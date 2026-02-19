"""Langfuse トレーシング統合."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from ai_agent_monitoring.core.config import Settings

logger = logging.getLogger(__name__)

# Langfuse がインストールされていない場合のフォールバック
try:
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    LANGFUSE_AVAILABLE = True
except ImportError:
    LANGFUSE_AVAILABLE = False
    logger.info("langfuse not installed. Tracing disabled.")

if TYPE_CHECKING:
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler


def _configure_langfuse_env(settings: Settings) -> None:
    """Settings の値を Langfuse が参照する環境変数に反映する."""
    if settings.langfuse_public_key:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    if settings.langfuse_secret_key:
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    if settings.langfuse_base_url:
        os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_base_url)


def create_langfuse_handler(
    settings: Settings,
    session_id: str = "",
    user_id: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> LangfuseCallbackHandler | None:
    """Langfuse CallbackHandler を生成.

    Langfuse v3 では CallbackHandler はグローバルクライアントを使用する。
    設定値は環境変数経由で渡す。

    Args:
        settings: アプリケーション設定
        session_id: 調査セッションID（investigation_id）
        user_id: トリガーしたユーザID
        tags: トレースに付与するタグ
        metadata: トレースに付与するメタデータ

    Returns:
        LangfuseCallbackHandler or None
    """
    if not LANGFUSE_AVAILABLE:
        return None

    if not settings.langfuse_enabled:
        logger.debug("Langfuse tracing is disabled")
        return None

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning("Langfuse keys not configured. Tracing disabled.")
        return None

    # Langfuse v3 は環境変数からクライアント設定を読む
    _configure_langfuse_env(settings)

    # trace_context でセッションID等をトレースに紐付ける
    # Langfuseはtrace_idとして32文字の16進数(UUID形式)を要求する
    trace_context: dict[str, Any] = {}
    if session_id:
        # session_idをUUID名前空間でハッシュして有効なtrace_idを生成
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
        valid_trace_id = str(uuid.uuid5(namespace, session_id)).replace("-", "")
        trace_context["trace_id"] = valid_trace_id
        trace_context["session_id"] = session_id  # 元のsession_idも保持

    handler = LangfuseCallbackHandler(
        trace_context=trace_context or None,  # type: ignore[arg-type]
    )
    logger.debug("Langfuse handler created: session=%s", session_id)
    return handler


def build_runnable_config(
    settings: Settings,
    investigation_id: str = "",
    trigger_type: str = "",
    extra_tags: list[str] | None = None,
) -> dict[str, Any]:
    """LangGraph invoke 用の config を構築.

    Langfuse が有効ならコールバックを含め、無効なら空の config を返す。
    run_id を設定することで、同じ調査のすべてのLLM呼び出しが
    同じLangfuseトレースに収まるようにする。

    Args:
        settings: アプリケーション設定
        investigation_id: 調査ID（Langfuse session_id および run_id に対応）
        trigger_type: "alert" or "user_query"
        extra_tags: 追加タグ

    Returns:
        LangGraph の invoke に渡す config dict
    """
    tags = [trigger_type] if trigger_type else []
    if extra_tags:
        tags.extend(extra_tags)

    handler = create_langfuse_handler(
        settings=settings,
        session_id=investigation_id,
        tags=tags,
        metadata={
            "investigation_id": investigation_id,
            "trigger_type": trigger_type,
        },
    )

    config: dict[str, Any] = {}
    if handler:
        config["callbacks"] = [handler]

    # run_id を設定して同じトレースに収める
    # LangChain/LangGraphはrun_idをLangfuseのtrace_idとして使用する
    if investigation_id:
        # investigation_idをUUID形式に変換（LangChainはUUID形式のrun_idを要求）
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
        run_uuid = uuid.uuid5(namespace, investigation_id)
        config["run_id"] = run_uuid

    return config
