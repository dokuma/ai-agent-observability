"""API リクエスト/レスポンススキーマ."""

from datetime import datetime

from pydantic import BaseModel, Field

from ai_agent_monitoring.core.models import RootCause

# ---- AlertManager Webhook ----

class AlertManagerAlert(BaseModel):
    """AlertManager Webhook のアラート形式."""

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str  # noqa: N815 (AlertManager形式に合わせる)
    endsAt: str = ""  # noqa: N815


class AlertManagerWebhookPayload(BaseModel):
    """AlertManager Webhook のペイロード形式."""

    version: str = "4"
    status: str = "firing"
    alerts: list[AlertManagerAlert] = Field(default_factory=list)
    groupLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonAnnotations: dict[str, str] = Field(default_factory=dict)  # noqa: N815


# ---- ユーザクエリ ----

class UserQueryRequest(BaseModel):
    """ユーザ自然言語クエリのリクエスト."""

    query: str = Field(min_length=1, description="自然言語の問い合わせ")
    target_instances: list[str] = Field(
        default_factory=list,
        description="調査対象インスタンス（省略時は全て）",
    )


class UserQueryResponse(BaseModel):
    """ユーザクエリの非同期レスポンス."""

    investigation_id: str
    status: str
    message: str


# ---- 調査ステータス ----

class InvestigationStatus(BaseModel):
    """調査の進捗状態."""

    investigation_id: str
    status: str  # "running" | "completed" | "failed"
    trigger_type: str
    iteration_count: int = 0
    created_at: datetime
    completed_at: datetime | None = None


# ---- RCAレポート ----

class RCAReportResponse(BaseModel):
    """RCAレポートのAPIレスポンス."""

    investigation_id: str
    trigger_type: str
    root_causes: list[RootCause] = Field(default_factory=list)
    metrics_summary: str = ""
    logs_summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    markdown: str = ""
    created_at: datetime


# ---- ヘルスチェック ----

class HealthResponse(BaseModel):
    """ヘルスチェックレスポンス."""

    status: str  # "healthy" | "degraded" | "unhealthy"
    mcp_servers: dict[str, bool] = Field(default_factory=dict)
    version: str = "0.1.0"
