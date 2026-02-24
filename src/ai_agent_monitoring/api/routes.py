"""API ルーター定義."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from openai import RateLimitError

from ai_agent_monitoring.api.dependencies import app_state
from ai_agent_monitoring.api.schemas import (
    AlertManagerWebhookPayload,
    HealthResponse,
    InvestigationStatus,
    RCAReportResponse,
    UserQueryRequest,
    UserQueryResponse,
)
from ai_agent_monitoring.core.models import Alert, Severity, TriggerType, UserQuery
from ai_agent_monitoring.core.tracing import build_runnable_config

logger = logging.getLogger(__name__)

router = APIRouter()

# ヘルスチェック再実行の並行呼び出し保護
_health_refresh_lock = asyncio.Lock()


# ---- ヘルスチェック ----


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """API + MCP Server のヘルスチェック."""
    if not app_state.registry:
        return HealthResponse(status="unhealthy", mcp_servers={})

    mcp_status = await app_state.registry.health_check()
    all_healthy = all(mcp_status.values())
    any_healthy = any(mcp_status.values())

    if all_healthy:
        status = "healthy"
    elif any_healthy:
        status = "degraded"
    else:
        status = "unhealthy"

    return HealthResponse(status=status, mcp_servers=mcp_status)


# ---- AlertManager Webhook ----


@router.post("/webhook/alertmanager", response_model=UserQueryResponse)
async def receive_alert(
    payload: AlertManagerWebhookPayload,
    background_tasks: BackgroundTasks,
) -> UserQueryResponse:
    """AlertManager からの Webhook を受信し調査を開始."""
    if not payload.alerts:
        raise HTTPException(status_code=400, detail="No alerts in payload")

    # 最初のアラートを処理（バッチ対応は将来拡張）
    am_alert = payload.alerts[0]
    alert = Alert(
        alert_name=am_alert.labels.get("alertname", "unknown"),
        severity=Severity(am_alert.labels.get("severity", "warning")),
        instance=am_alert.labels.get("instance", "unknown"),
        summary=am_alert.annotations.get("summary", ""),
        description=am_alert.annotations.get("description", ""),
        labels=am_alert.labels,
        annotations=am_alert.annotations,
        starts_at=datetime.fromisoformat(am_alert.startsAt),
        ends_at=datetime.fromisoformat(am_alert.endsAt) if am_alert.endsAt else None,
    )

    inv_id = app_state.create_investigation("alert")
    background_tasks.add_task(_run_alert_investigation, inv_id, alert)

    return UserQueryResponse(
        investigation_id=inv_id,
        status="running",
        message=f"調査を開始しました: {alert.alert_name}",
    )


# ---- ユーザクエリ ----


@router.post("/query", response_model=UserQueryResponse)
async def submit_query(
    request: UserQueryRequest,
    background_tasks: BackgroundTasks,
) -> UserQueryResponse:
    """ユーザの自然言語クエリを受け付け調査を開始."""
    user_query = UserQuery(
        raw_input=request.query,
        target_instances=request.target_instances,
    )

    inv_id = app_state.create_investigation("user_query")
    background_tasks.add_task(_run_user_query_investigation, inv_id, user_query)

    return UserQueryResponse(
        investigation_id=inv_id,
        status="running",
        message="調査を開始しました",
    )


# ---- 調査ステータス ----


@router.get("/investigations/{investigation_id}", response_model=InvestigationStatus)
async def get_investigation_status(investigation_id: str) -> InvestigationStatus:
    """調査の進捗状態を取得."""
    record = app_state.get_investigation(investigation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Investigation not found")

    return InvestigationStatus(
        investigation_id=record.investigation_id,
        status=record.status,
        trigger_type=record.trigger_type,
        iteration_count=record.iteration_count,
        current_stage=record.current_stage,
        error=record.error,
        created_at=record.created_at,
        completed_at=record.completed_at,
        mcp_status=record.mcp_status,
    )


# ---- RCAレポート取得 ----


@router.get("/investigations/{investigation_id}/report", response_model=RCAReportResponse)
async def get_investigation_report(investigation_id: str) -> RCAReportResponse:
    """完了した調査のRCAレポートを取得."""
    record = app_state.get_investigation(investigation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if record.status == "running":
        raise HTTPException(status_code=409, detail="Investigation still running")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=f"Investigation failed: {record.error}")

    if not record.rca_report:
        raise HTTPException(status_code=404, detail="Report not available")

    report = record.rca_report
    return RCAReportResponse(
        investigation_id=investigation_id,
        trigger_type=report.trigger_type,
        root_causes=report.root_causes,
        metrics_summary=report.metrics_summary,
        logs_summary=report.logs_summary,
        recommendations=report.recommendations,
        markdown=report.markdown,
        created_at=report.created_at,
    )


# ---- バックグラウンドタスク ----


async def _refresh_orchestrator_health(inv_id: str) -> dict[str, bool]:
    """調査開始前にMCPヘルスチェックを再実行しグラフを再構築.

    並行呼び出しをロックで保護し、結果をInvestigationRecordに保存する。
    """
    async with _health_refresh_lock:
        registry = app_state.registry
        orchestrator = app_state.orchestrator
        if not registry or not orchestrator:
            return {}

        mcp_status = await registry.health_check()
        orchestrator.refresh_health(registry)
        logger.info("MCP health refreshed before investigation %s: %s", inv_id, mcp_status)

        record = app_state.get_investigation(inv_id)
        if record:
            record.mcp_status = mcp_status

        return mcp_status


async def _run_alert_investigation(inv_id: str, alert: Alert) -> None:
    """アラート起動の調査をバックグラウンドで実行."""
    if not app_state.orchestrator:
        app_state.fail_investigation(inv_id, "Orchestrator not initialized")
        return

    # 調査開始前にMCPヘルスチェックを再実行
    await _refresh_orchestrator_health(inv_id)

    timeout = app_state.settings.investigation_timeout_seconds

    try:
        logger.info("Starting alert investigation: %s (%s)", inv_id, alert.alert_name)
        compiled = app_state.orchestrator.compile()
        config = build_runnable_config(
            settings=app_state.settings,
            investigation_id=inv_id,
            trigger_type="alert",
            extra_tags=[alert.alert_name, alert.severity],
        )

        # タイムアウト付きで実行
        task = asyncio.create_task(
            compiled.ainvoke(
                {
                    "investigation_id": inv_id,
                    "trigger_type": TriggerType.ALERT,
                    "alert": alert,
                    "messages": [],
                },
                config=config,
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            rca_report = result.get("rca_report")
            app_state.complete_investigation(inv_id, rca_report=rca_report)
            logger.info("Investigation completed: %s", inv_id)
        except TimeoutError:
            logger.warning("Investigation timed out after %ds: %s", timeout, inv_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Investigation task cancelled: %s", inv_id)
            app_state.fail_investigation(inv_id, f"調査がタイムアウトしました ({timeout}秒)")
    except asyncio.CancelledError:
        logger.info("Investigation cancelled: %s", inv_id)
        app_state.fail_investigation(inv_id, "調査がキャンセルされました")
    except RateLimitError as e:
        logger.warning("Investigation rate-limited: %s - %s", inv_id, e)
        app_state.fail_investigation(
            inv_id,
            "LLM APIのレートリミットにより調査を中断しました。しばらく待ってから再試行してください。",
        )
    except Exception as e:
        logger.exception("Investigation failed: %s", inv_id)
        app_state.fail_investigation(inv_id, str(e))


async def _run_user_query_investigation(inv_id: str, user_query: UserQuery) -> None:
    """ユーザクエリ起動の調査をバックグラウンドで実行."""
    if not app_state.orchestrator:
        app_state.fail_investigation(inv_id, "Orchestrator not initialized")
        return

    # 調査開始前にMCPヘルスチェックを再実行
    await _refresh_orchestrator_health(inv_id)

    timeout = app_state.settings.investigation_timeout_seconds

    try:
        logger.info("Starting user query investigation: %s", inv_id)
        compiled = app_state.orchestrator.compile()
        config = build_runnable_config(
            settings=app_state.settings,
            investigation_id=inv_id,
            trigger_type="user_query",
        )

        # タイムアウト付きで実行
        task = asyncio.create_task(
            compiled.ainvoke(
                {
                    "investigation_id": inv_id,
                    "trigger_type": TriggerType.USER_QUERY,
                    "user_query": user_query,
                    "messages": [],
                },
                config=config,
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            rca_report = result.get("rca_report")
            app_state.complete_investigation(inv_id, rca_report=rca_report)
            logger.info("Investigation completed: %s", inv_id)
        except TimeoutError:
            logger.warning("Investigation timed out after %ds: %s", timeout, inv_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Investigation task cancelled: %s", inv_id)
            app_state.fail_investigation(inv_id, f"調査がタイムアウトしました ({timeout}秒)")
    except asyncio.CancelledError:
        logger.info("Investigation cancelled: %s", inv_id)
        app_state.fail_investigation(inv_id, "調査がキャンセルされました")
    except RateLimitError as e:
        logger.warning("Investigation rate-limited: %s - %s", inv_id, e)
        app_state.fail_investigation(
            inv_id,
            "LLM APIのレートリミットにより調査を中断しました。しばらく待ってから再試行してください。",
        )
    except Exception as e:
        logger.exception("Investigation failed: %s", inv_id)
        app_state.fail_investigation(inv_id, str(e))
