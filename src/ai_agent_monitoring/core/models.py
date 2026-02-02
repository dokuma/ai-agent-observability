"""AI Agent モニタリングシステムの共通Pydanticモデル."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TriggerType(StrEnum):
    """ワークフローの起動トリガー種別."""

    ALERT = "alert"
    USER_QUERY = "user_query"


class UserQuery(BaseModel):
    """ユーザからの自然言語プロンプト入力."""

    raw_input: str
    parsed_intent: str = ""
    target_instances: list[str] = Field(default_factory=list)
    time_reference: str = ""
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None


class Severity(StrEnum):
    """アラート重要度."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Alert(BaseModel):
    """AlertManagerから受信するアラート."""

    alert_name: str
    severity: Severity
    instance: str
    summary: str
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime
    ends_at: datetime | None = None


class MetricDataPoint(BaseModel):
    """メトリクスのデータポイント."""

    timestamp: datetime
    value: float


class MetricsResult(BaseModel):
    """Prometheus メトリクス分析結果."""

    query: str
    data_points: list[MetricDataPoint] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    summary: str = ""


class LogEntry(BaseModel):
    """ログエントリ."""

    timestamp: datetime
    level: str
    message: str
    labels: dict[str, str] = Field(default_factory=dict)


class LogsResult(BaseModel):
    """Loki ログ分析結果."""

    query: str
    entries: list[LogEntry] = Field(default_factory=list)
    error_patterns: list[str] = Field(default_factory=list)
    summary: str = ""


class RootCause(BaseModel):
    """特定された根本原因."""

    description: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class PanelSnapshot(BaseModel):
    """Grafana パネルのスナップショット画像."""

    dashboard_uid: str
    panel_id: int
    query: str = ""
    image_path: str = ""
    caption: str = ""


class LogExcerpt(BaseModel):
    """レポートに含めるログ抜粋."""

    query: str
    entries: list[LogEntry] = Field(default_factory=list)
    caption: str = ""


class RCAReport(BaseModel):
    """根本原因分析レポート."""

    trigger_type: TriggerType
    alert: Alert | None = None
    user_query: UserQuery | None = None
    root_causes: list[RootCause] = Field(default_factory=list)
    metrics_summary: str = ""
    logs_summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    panel_snapshots: list[PanelSnapshot] = Field(default_factory=list)
    log_excerpts: list[LogExcerpt] = Field(default_factory=list)
    markdown: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
