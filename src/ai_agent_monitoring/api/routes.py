"""API ルーター定義."""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from langgraph.types import Command
from openai import RateLimitError

from ai_agent_monitoring.api.dependencies import app_state
from ai_agent_monitoring.api.schemas import (
    AlertManagerWebhookPayload,
    HealthResponse,
    InvestigationStatus,
    RCAReportResponse,
    ReportListResponse,
    ReportSearchRequest,
    ReportSearchResponse,
    RetryRequest,
    RetryType,
    UserInputRequest,
    UserQueryRequest,
    UserQueryResponse,
)
from ai_agent_monitoring.core.models import Alert, Severity, StoredRCAReport, TriggerType, UserQuery
from ai_agent_monitoring.core.tracing import build_runnable_config


def _extract_env_json(result: dict[str, Any]) -> str:
    """AgentState 結果から EnvironmentContext の JSON を取得."""
    env = result.get("environment")
    if env is None:
        return ""
    try:
        return str(env.model_dump_json())
    except Exception:
        return ""


logger = logging.getLogger(__name__)

router = APIRouter()

# retry パターン検出用正規表現
_RETRY_PATTERN = re.compile(r"再調査|やり直|別の角度|深掘|もっと詳しく|追加で調査|続けて")
_REINVESTIGATE_PATTERN = re.compile(r"再調査|やり直|別の角度")
_CONTINUE_PATTERN = re.compile(r"深掘|もっと詳しく|追加で調査|続けて")

# フォローアップ実行パターン（RCAレポートの推奨アクションを実行する意図）
_FOLLOWUP_PATTERN = re.compile(r"実行して|実施して|お願いします|やってください|進めて|試して")

# 回顧的クエリ検出用正規表現
_RETROSPECTIVE_PATTERN = re.compile(
    r"前回|過去の|以前の|先ほど|さっき|履歴|"
    r"(実行|使用|使った).*(クエリ|コマンド)|"
    r"(クエリ|コマンド|結果).*(一覧|リスト|出力|表示|教えて)|"
    r"何を(実行|調査|確認)し|"
    r"どんな(クエリ|コマンド|調査)を"
)


def _is_retrospective_query(query: str) -> bool:
    """過去の調査結果・実行クエリの参照を目的としたクエリかどうか判定."""
    return bool(_RETROSPECTIVE_PATTERN.search(query))


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


@router.get("/health/mcp-diagnose")
async def mcp_diagnose() -> dict[str, Any]:
    """MCPプロトコルレベルの診断を実行.

    HTTPヘルスチェックに加えて、実際のMCPセッション初期化を試行し、
    プロトコルレベルの互換性問題を検出する。
    """
    if not app_state.registry:
        return {"error": "Registry not initialized"}
    return await app_state.registry.diagnose_mcp()


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


def _detect_retry_pattern(query: str) -> str | None:
    """正規表現で retry 系パターンを検出.

    Returns:
        "retry:reinvestigate" or "retry:continue_investigation" or None
    """
    if not _RETRY_PATTERN.search(query):
        return None
    if _REINVESTIGATE_PATTERN.search(query):
        return "retry:reinvestigate"
    return "retry:continue_investigation"


def _build_followup_query(query: str) -> str | None:
    """フォローアップ実行パターンを検出し、直前のRCA recommendations を組み込んだクエリを構築.

    Returns:
        recommendations を含むクエリ文字列、またはフォローアップでない場合 None
    """
    if not _FOLLOWUP_PATTERN.search(query):
        return None

    # 直近の completed 調査から recommendations を取得
    for record in reversed(list(app_state.investigations.values())):
        if record.status == "completed" and record.rca_report:
            recommendations = record.rca_report.recommendations
            if recommendations:
                recs_text = "\n".join(f"- {r}" for r in recommendations)
                return (
                    f"前回の調査で以下の推奨アクションが提示されました:\n{recs_text}\n\n"
                    f"ユーザの指示: {query}\n\n"
                    "上記の推奨アクションに基づいて追加の調査・確認を実施してください。"
                )
            break
    return None


@router.post("/query", response_model=UserQueryResponse)
async def submit_query(
    request: UserQueryRequest,
    background_tasks: BackgroundTasks,
) -> UserQueryResponse:
    """ユーザの自然言語クエリを受け付け、Search-First フローで処理.

    1. retry パターン検出 → 該当すれば既存の retry 処理へ
    2. レポートストアで検索実行（BM25 + ベクトル検索）
    3. 検索結果が十分（スコア閾値超え）→ knowledge_search_agent で回答生成
    4. 検索結果が不十分 → 新規調査開始
    """
    report_search_timeout = app_state.settings.report_search_timeout_seconds
    threshold = app_state.settings.search_relevance_threshold

    # ---- Step 1: retry パターン検出 ----
    retry_intent = _detect_retry_pattern(request.query)
    if retry_intent:
        retry_type_str = retry_intent.split(":", 1)[1]
        try:
            retry_type = RetryType(retry_type_str)
        except ValueError:
            retry_type = RetryType.REGENERATE_RCA

        # 直近の completed 調査を探す
        target_record = None
        for record in reversed(list(app_state.investigations.values())):
            if record.status == "completed" and record.compiled_graph and record.graph_config:
                target_record = record
                break

        if target_record and target_record.graph_config is not None:
            target_record.status = "running"
            target_record.completed_at = None
            target_record.error = ""
            target_record.current_stage = "やり直し中"

            background_tasks.add_task(
                _retry_investigation,
                target_record.investigation_id,
                target_record.compiled_graph,
                target_record.graph_config,
                retry_type,
                request.query,
            )

            return UserQueryResponse(
                investigation_id=target_record.investigation_id,
                status="running",
                message=f"調査をやり直します ({retry_type.value})",
            )
        # completed 調査が見つからない場合は新規調査にフォールスルー

    # ---- Step 1.5: フォローアップ実行パターン検出 ----
    followup_query = _build_followup_query(request.query)
    if followup_query:
        user_query = UserQuery(
            raw_input=followup_query,
            target_instances=request.target_instances,
        )
        inv_id = app_state.create_investigation("user_query")
        background_tasks.add_task(_run_user_query_investigation, inv_id, user_query)
        return UserQueryResponse(
            investigation_id=inv_id,
            status="running",
            message="前回の推奨アクションに基づいて追加調査を開始します",
        )

    # ---- Step 2: レポート検索 + ObservationStore 検索（Search-First） ----
    search_miss_message: str | None = None
    observation_context = ""

    # ObservationStore 検索（レポート検索と並行して実行）
    obs_task = asyncio.create_task(_search_observation_store(request.query))

    if app_state.vector_store and app_state.knowledge_search_agent:
        # ベクトル検索で関連レポートを検索
        try:
            search_results = await app_state.vector_store.search(request.query, top_k=5)

            # ObservationStore の結果を取得
            observation_context = await obs_task

            has_relevant_results = bool(search_results) and any(r.score > 0 for r in search_results)

            if has_relevant_results and search_results:
                # ---- Step 3: バックグラウンドでレポート検索 → 不十分なら調査へ自動移行 ----
                logger.info(
                    "Search-first: relevant results found (top_score=%.3f, threshold=%.3f), routing to report_search",
                    search_results[0].score if search_results else 0,
                    threshold,
                )
                inv_id = app_state.create_investigation("report_search")
                app_state.update_investigation_stage(inv_id, "レポート検索中")
                is_retrospective = _is_retrospective_query(request.query)

                background_tasks.add_task(
                    _run_report_search,
                    inv_id,
                    request.query,
                    report_search_timeout,
                    observation_context,
                    is_retrospective,
                )
                return UserQueryResponse(
                    investigation_id=inv_id,
                    status="running",
                    message="過去のレポートと観測データから回答を検索中です...",
                    routed_to="report_search",
                )
            else:
                top_score = search_results[0].score if search_results else 0
                logger.info(
                    "Search-first: no relevant results (top_score=%.3f, threshold=%.3f), starting investigation",
                    top_score,
                    threshold,
                )
                search_miss_message = "過去のレポートに関連する情報が見つからなかったため、新しく調査を開始します。"
        except Exception:
            logger.warning("Search-first: search failed, falling back to investigation", exc_info=True)
            search_miss_message = None

    # ObservationStore の結果がまだ未取得の場合は待機
    if not obs_task.done():
        observation_context = await obs_task

    # ---- Step 4: 新規調査開始 ----
    user_query = UserQuery(
        raw_input=request.query,
        target_instances=request.target_instances,
    )

    inv_id = app_state.create_investigation("user_query")
    background_tasks.add_task(_run_user_query_investigation, inv_id, user_query)

    # 検索不一致の場合はその旨をメッセージに含める
    message = search_miss_message if search_miss_message else "調査を開始しました"

    return UserQueryResponse(
        investigation_id=inv_id,
        status="running",
        message=message,
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
        stage_detail=record.stage_detail,
        error=record.error,
        created_at=record.created_at,
        completed_at=record.completed_at,
        mcp_status=record.mcp_status,
        pending_input=record.pending_input,
        report_search_answer=record.report_search_answer,
        followup_investigation_id=record.followup_investigation_id,
    )


# ---- ユーザ入力（interrupt resume） ----


@router.post("/investigations/{investigation_id}/input")
async def submit_user_input(
    investigation_id: str,
    request: UserInputRequest,
    background_tasks: BackgroundTasks,
) -> UserQueryResponse:
    """interrupt中の調査に対してユーザ入力を送信し再開."""
    record = app_state.get_investigation(investigation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if record.status != "waiting_for_input":
        raise HTTPException(
            status_code=409,
            detail=f"Investigation is not waiting for input (status={record.status})",
        )
    if not record.compiled_graph or not record.graph_config:
        raise HTTPException(
            status_code=500,
            detail="Resume state not available",
        )

    # running に戻す
    record.status = "running"
    record.pending_input = None
    record.current_stage = "ユーザ入力を受理、調査を再開中"

    background_tasks.add_task(
        _resume_investigation,
        investigation_id,
        record.compiled_graph,
        record.graph_config,
        request.value,
    )

    return UserQueryResponse(
        investigation_id=investigation_id,
        status="running",
        message="ユーザ入力を受理しました。調査を再開します。",
    )


# ---- 調査キャンセル ----


@router.post("/investigations/{investigation_id}/cancel")
async def cancel_investigation(investigation_id: str) -> dict[str, str]:
    """実行中の調査をキャンセルする."""
    record = app_state.get_investigation(investigation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if record.status not in ("running", "waiting_for_input"):
        raise HTTPException(
            status_code=409,
            detail=f"Investigation is not running (status={record.status})",
        )

    # asyncio.Task をキャンセル
    if record.task is not None:
        record.task.cancel()

    record.status = "cancelled"
    record.completed_at = datetime.now()
    record.error = "ユーザによりキャンセルされました"
    record.current_stage = "キャンセル済み"

    logger.info("Investigation cancelled by user: %s", investigation_id)
    return {"status": "cancelled"}


# ---- チェックポイントやり直し ----


@router.post("/investigations/{investigation_id}/retry", response_model=UserQueryResponse)
async def retry_investigation(
    investigation_id: str,
    request: RetryRequest,
    background_tasks: BackgroundTasks,
) -> UserQueryResponse:
    """完了済み調査をチェックポイントからやり直す."""
    record = app_state.get_investigation(investigation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Investigation not found")
    if record.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Investigation is not completed (status={record.status})",
        )
    if not record.compiled_graph or not record.graph_config:
        raise HTTPException(
            status_code=410,
            detail="Graph state no longer available (Gone)",
        )

    # running に戻す
    record.status = "running"
    record.completed_at = None
    record.error = ""
    record.current_stage = "やり直し中"

    background_tasks.add_task(
        _retry_investigation,
        investigation_id,
        record.compiled_graph,
        record.graph_config,
        request.retry_type,
        request.feedback,
    )

    return UserQueryResponse(
        investigation_id=investigation_id,
        status="running",
        message=f"調査をやり直します ({request.retry_type.value})",
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
        logger.debug("MCP health refreshed before investigation %s: %s", inv_id, mcp_status)

        record = app_state.get_investigation(inv_id)
        if record:
            record.mcp_status = mcp_status

        return mcp_status


_NEEDS_INVESTIGATION_MARKER = "[NEEDS_INVESTIGATION]"


async def _search_observation_store(query: str) -> str:
    """ObservationStore から過去の類似観測を検索しテキストを返す.

    Returns:
        観測データのテキスト。結果なしまたはストア未初期化時は空文字列。
    """
    if not app_state.observation_store:
        logger.info("ObservationStore search skipped: store not initialized")
        return ""
    try:
        from datetime import UTC, datetime

        logger.info("ObservationStore search starting for query: %s", query[:100])
        results = await app_state.observation_store.search_similar(
            query=query,
            top_k=5,
        )
        if not results:
            logger.info("ObservationStore search returned 0 results")
            return ""
        lines: list[str] = []
        for r in results:
            age_dt = datetime.fromtimestamp(r.created_at_ts, tz=UTC)
            age_str = age_dt.strftime("%Y-%m-%d %H:%M")
            lines.append(f"- [{r.observation_type}] {age_str} (score={r.score:.2f}): {r.summary}")
        logger.info("ObservationStore search returned %d results for query: %s", len(results), query[:100])
        return "\n".join(lines)
    except Exception:
        logger.warning("ObservationStore search failed", exc_info=True)
        return ""


async def _run_report_search(
    inv_id: str,
    query: str,
    timeout: int,
    observation_context: str = "",
    is_retrospective: bool = False,
) -> None:
    """レポート検索をバックグラウンドで実行し、結果を InvestigationRecord に保存.

    回答に [NEEDS_INVESTIGATION] マーカーが含まれる場合は、
    過去レポートの部分回答を保持しつつ新規調査を自動開始する。
    ただし is_retrospective=True の場合は [NEEDS_INVESTIGATION] を除去し、
    フォローアップ調査を開始しない。
    """
    if not app_state.knowledge_search_agent:
        app_state.fail_investigation(inv_id, "Report search agent not initialized")
        return

    try:
        result = await asyncio.wait_for(
            app_state.knowledge_search_agent.search_and_answer(
                query=query,
                observation_context=observation_context,
            ),
            timeout=timeout,
        )
        answer = result.answer
        if not answer or answer.strip() in ("{}", "[]", "null"):
            answer = "該当するRCAレポートが見つかりませんでした。新しく調査を開始してください。"

        # [NEEDS_INVESTIGATION] マーカー検出 → 調査へ自動移行
        # ただし回顧的クエリの場合は抑制
        if _NEEDS_INVESTIGATION_MARKER in answer and is_retrospective:
            logger.info(
                "Retrospective query detected for %s, suppressing [NEEDS_INVESTIGATION] followup",
                inv_id,
            )
            answer = answer.replace(_NEEDS_INVESTIGATION_MARKER, "").rstrip()
        elif _NEEDS_INVESTIGATION_MARKER in answer:
            stripped_answer = answer.replace(_NEEDS_INVESTIGATION_MARKER, "").rstrip()
            logger.info(
                "Report search flagged [NEEDS_INVESTIGATION] for %s, starting followup investigation",
                inv_id,
            )
            # 新規調査を開始
            followup_id = app_state.create_investigation("user_query")
            user_query = UserQuery(raw_input=query)

            record = app_state.get_investigation(inv_id)
            if record:
                record.status = "completed"
                record.completed_at = datetime.now()
                record.report_search_answer = stripped_answer
                record.followup_investigation_id = followup_id

            # フォローアップ調査をバックグラウンドで開始
            followup_task = asyncio.create_task(_run_user_query_investigation(followup_id, user_query))
            followup_record = app_state.get_investigation(followup_id)
            if followup_record:
                followup_record.task = followup_task
            return

        record = app_state.get_investigation(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.report_search_answer = answer
        logger.info("Report search completed for %s: answer_length=%d", inv_id, len(answer))
    except TimeoutError:
        logger.warning("Report search timed out after %ds: %s", timeout, inv_id)
        record = app_state.get_investigation(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.report_search_answer = (
                "⏰ 過去レポートの検索に時間がかかりました。\n\n"
                "以下のいずれかをお試しください:\n"
                "- もう少し具体的なキーワードで再度質問する\n"
                "- 「新しく調査して」と入力して新規調査を開始する"
            )
    except Exception:
        logger.warning("Report search failed: %s", inv_id, exc_info=True)
        record = app_state.get_investigation(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.report_search_answer = (
                "❌ 過去レポートの検索中にエラーが発生しました。\n\n"
                "「新しく調査して」と入力して新規調査を開始できます。"
            )


def _check_interrupt(result: dict[str, Any], inv_id: str, compiled: Any, config: dict[str, Any]) -> bool:
    """invoke結果にinterruptが含まれているかチェックし、含まれていればwaiting_for_inputに遷移.

    Returns:
        True if interrupt was found and handled, False otherwise.
    """
    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        interrupt_value = interrupts[0].value if interrupts else {}
        app_state.set_waiting_for_input(
            inv_id,
            pending_input=interrupt_value,
            compiled_graph=compiled,
            config=config,
        )
        return True
    return False


async def _run_investigation_loop(
    inv_id: str,
    compiled: Any,
    config: dict[str, Any],
    initial_state: dict[str, Any],
) -> None:
    """調査をタイムアウト付きで実行し、interrupt/完了/エラーを処理する共通関数."""
    timeout = app_state.settings.investigation_timeout_seconds

    try:
        task = asyncio.create_task(compiled.ainvoke(initial_state, config=config))

        # タスク参照を保存（キャンセル用）
        record = app_state.get_investigation(inv_id)
        if record:
            record.task = task

        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

            # interrupt チェック
            if _check_interrupt(result, inv_id, compiled, config):
                return

            rca_report = result.get("rca_report")
            env_json = _extract_env_json(result)
            await app_state.complete_investigation(inv_id, rca_report=rca_report, environment_json=env_json)
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
        # キャンセルエンドポイントから呼ばれた場合は既に cancelled 状態
        inv_record = app_state.get_investigation(inv_id)
        if inv_record and inv_record.status != "cancelled":
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


async def _run_alert_investigation(inv_id: str, alert: Alert) -> None:
    """アラート起動の調査をバックグラウンドで実行."""
    if not app_state.orchestrator:
        app_state.fail_investigation(inv_id, "Orchestrator not initialized")
        return

    # 調査開始前にMCPヘルスチェックを再実行
    await _refresh_orchestrator_health(inv_id)

    logger.info("Starting alert investigation: %s (%s)", inv_id, alert.alert_name)
    compiled = app_state.orchestrator.compile(checkpointer=app_state.checkpointer)
    config = build_runnable_config(
        settings=app_state.settings,
        investigation_id=inv_id,
        trigger_type="alert",
        extra_tags=[alert.alert_name, alert.severity],
    )

    await _run_investigation_loop(
        inv_id,
        compiled,
        config,
        {
            "investigation_id": inv_id,
            "trigger_type": TriggerType.ALERT,
            "alert": alert,
            "messages": [],
        },
    )


async def _run_user_query_investigation(inv_id: str, user_query: UserQuery) -> None:
    """ユーザクエリ起動の調査をバックグラウンドで実行."""
    if not app_state.orchestrator:
        app_state.fail_investigation(inv_id, "Orchestrator not initialized")
        return

    # 調査開始前にMCPヘルスチェックを再実行
    await _refresh_orchestrator_health(inv_id)

    logger.info("Starting user query investigation: %s", inv_id)
    compiled = app_state.orchestrator.compile(checkpointer=app_state.checkpointer)
    config = build_runnable_config(
        settings=app_state.settings,
        investigation_id=inv_id,
        trigger_type="user_query",
    )

    await _run_investigation_loop(
        inv_id,
        compiled,
        config,
        {
            "investigation_id": inv_id,
            "trigger_type": TriggerType.USER_QUERY,
            "user_query": user_query,
            "messages": [],
        },
    )


async def _resume_investigation(
    inv_id: str,
    compiled: Any,
    config: dict[str, Any],
    user_input: Any,
) -> None:
    """interrupt後の調査をCommand(resume=)で再開."""
    timeout = app_state.settings.investigation_timeout_seconds

    try:
        logger.info("Resuming investigation: %s", inv_id)
        task = asyncio.create_task(compiled.ainvoke(Command(resume=user_input), config=config))
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

            # 再度 interrupt チェック（prometheus選択後にloki選択が必要な場合）
            if _check_interrupt(result, inv_id, compiled, config):
                return

            rca_report = result.get("rca_report")
            env_json = _extract_env_json(result)
            await app_state.complete_investigation(inv_id, rca_report=rca_report, environment_json=env_json)
            logger.info("Investigation completed after resume: %s", inv_id)
        except TimeoutError:
            logger.warning("Investigation timed out after resume %ds: %s", timeout, inv_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Investigation task cancelled after resume: %s", inv_id)
            app_state.fail_investigation(inv_id, f"調査がタイムアウトしました ({timeout}秒)")
    except asyncio.CancelledError:
        logger.info("Investigation cancelled after resume: %s", inv_id)
        app_state.fail_investigation(inv_id, "調査がキャンセルされました")
    except RateLimitError as e:
        logger.warning("Investigation rate-limited after resume: %s - %s", inv_id, e)
        app_state.fail_investigation(
            inv_id,
            "LLM APIのレートリミットにより調査を中断しました。しばらく待ってから再試行してください。",
        )
    except Exception as e:
        logger.exception("Investigation failed after resume: %s", inv_id)
        app_state.fail_investigation(inv_id, str(e))


async def _retry_investigation(
    inv_id: str,
    compiled: Any,
    config: dict[str, Any],
    retry_type: RetryType,
    feedback: str,
) -> None:
    """チェックポイントからやり直す."""
    timeout = app_state.settings.investigation_timeout_seconds

    from ai_agent_monitoring.core.state import EvaluationFeedback

    # retry_type に応じて state を書き換え
    values: dict[str, Any]
    if retry_type == RetryType.REGENERATE_RCA:
        values = {"investigation_complete": True}
    elif retry_type == RetryType.REINVESTIGATE:
        values = {
            "investigation_complete": False,
            "iteration_count": 0,
        }
        if feedback:
            values["evaluation_feedback"] = EvaluationFeedback(
                reasoning=feedback,
                additional_investigation_points=[feedback],
            )
    elif retry_type == RetryType.CONTINUE_INVESTIGATION:
        # 現在の state を取得して max_iterations を +1
        current_state = compiled.get_state(config)
        current_max = current_state.values.get("max_iterations", 3)
        values = {
            "investigation_complete": False,
            "max_iterations": current_max + 1,
        }
        if feedback:
            values["evaluation_feedback"] = EvaluationFeedback(
                reasoning=feedback,
                additional_investigation_points=[feedback],
            )
    else:
        app_state.fail_investigation(inv_id, f"Unknown retry_type: {retry_type}")
        return

    try:
        # evaluate_results ノードの出力として state を上書き
        compiled.update_state(config, values, as_node="evaluate_results")

        # 次のノードから再実行
        task = asyncio.create_task(compiled.ainvoke(None, config=config))
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

            if _check_interrupt(result, inv_id, compiled, config):
                return

            rca_report = result.get("rca_report")
            env_json = _extract_env_json(result)
            await app_state.complete_investigation(inv_id, rca_report=rca_report, environment_json=env_json)
            logger.info("Investigation completed after retry: %s (%s)", inv_id, retry_type)
        except TimeoutError:
            logger.warning("Investigation timed out after retry %ds: %s", timeout, inv_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Investigation task cancelled after retry: %s", inv_id)
            app_state.fail_investigation(inv_id, f"調査がタイムアウトしました ({timeout}秒)")
    except asyncio.CancelledError:
        logger.info("Investigation cancelled after retry: %s", inv_id)
        app_state.fail_investigation(inv_id, "調査がキャンセルされました")
    except RateLimitError as e:
        logger.warning("Investigation rate-limited after retry: %s - %s", inv_id, e)
        app_state.fail_investigation(
            inv_id,
            "LLM APIのレートリミットにより調査を中断しました。しばらく待ってから再試行してください。",
        )
    except Exception as e:
        logger.exception("Investigation failed after retry: %s", inv_id)
        app_state.fail_investigation(inv_id, str(e))


# ---- RCAレポート検索・一覧 ----


@router.post("/reports/search", response_model=ReportSearchResponse)
async def search_reports(request: ReportSearchRequest) -> ReportSearchResponse:
    """過去のRCAレポートを自然言語で検索し、LLMが要約回答を生成."""
    if not app_state.knowledge_search_agent:
        raise HTTPException(status_code=503, detail="Report search agent not initialized")
    return await app_state.knowledge_search_agent.search_and_answer(query=request.query, top_k=request.top_k)


@router.get("/reports", response_model=ReportListResponse)
async def list_reports(offset: int = 0, limit: int = 20) -> ReportListResponse:
    """保存済みRCAレポートの一覧を取得（ページング対応）."""
    if not app_state.vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    results, total = await app_state.vector_store.scroll(limit=limit, offset=offset)
    reports = []
    for r in results:
        stored = StoredRCAReport.from_qdrant_payload(r.payload)
        if stored:
            reports.append(stored.model_dump(mode="json"))
    return ReportListResponse(
        reports=reports,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/reports/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    """個別のRCAレポートを取得."""
    if not app_state.vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    result = await app_state.vector_store.retrieve(report_id)
    if not result:
        raise HTTPException(status_code=404, detail="Report not found")
    stored = StoredRCAReport.from_qdrant_payload(result.payload)
    if not stored:
        raise HTTPException(status_code=404, detail="Report not found")
    return stored.model_dump(mode="json")
