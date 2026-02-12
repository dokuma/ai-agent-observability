"""LangGraph AgentState 定義."""

from datetime import datetime
from typing import Annotated, Any

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from ai_agent_monitoring.core.models import (
    Alert,
    LogsResult,
    MetricsResult,
    RCAReport,
    TriggerType,
    UserQuery,
)


def _merge_list(left: list[Any], right: list[Any]) -> list[Any]:
    """リストをマージするreducer."""
    return left + right


class TimeRange(BaseModel):
    """調査対象の時間範囲."""

    start: datetime
    end: datetime


class DashboardInfo(BaseModel):
    """ダッシュボード情報."""

    uid: str
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    relevance_score: float = 0.0  # キーワードマッチングによる関連度スコア


class PanelQuery(BaseModel):
    """パネルから抽出されたクエリ情報."""

    panel_title: str = ""
    query: str
    query_type: str = "promql"  # "promql" | "logql"
    dashboard_uid: str = ""
    dashboard_title: str = ""


class EnvironmentContext(BaseModel):
    """監視環境のコンテキスト情報.

    Grafana MCPから取得した環境情報を格納する。
    調査計画の生成時に利用可能なメトリクス・ラベル・
    ターゲットを把握するために使用。
    """

    # データソース情報
    prometheus_datasource_uid: str = ""
    loki_datasource_uid: str = ""

    # 利用可能なメトリクスとラベル
    available_metrics: list[str] = Field(default_factory=list)
    available_labels: list[str] = Field(default_factory=list)
    available_jobs: list[str] = Field(default_factory=list)
    available_instances: list[str] = Field(default_factory=list)

    # Lokiのラベル情報
    loki_labels: list[str] = Field(default_factory=list)
    loki_jobs: list[str] = Field(default_factory=list)

    # 既存ダッシュボードから学習したクエリパターン
    example_promql_queries: list[str] = Field(default_factory=list)
    example_logql_queries: list[str] = Field(default_factory=list)

    # ダッシュボード探索用
    investigation_keywords: list[str] = Field(default_factory=list)
    available_dashboards: list[DashboardInfo] = Field(default_factory=list)
    explored_dashboard_uids: list[str] = Field(default_factory=list)
    discovered_panel_queries: list[PanelQuery] = Field(default_factory=list)


class EvaluationFeedback(BaseModel):
    """調査結果の評価フィードバック.

    INSUFFICIENTと判定された場合に、不足している情報や
    追加で調査すべき観点を構造化して保持する。
    次のイテレーションの調査計画に反映される。
    """

    missing_information: list[str] = Field(default_factory=list)
    additional_investigation_points: list[str] = Field(default_factory=list)
    previous_queries_attempted: list[str] = Field(default_factory=list)
    reasoning: str = ""


class InvestigationPlan(BaseModel):
    """Orchestratorが生成する調査計画."""

    # データソースUID（クエリ実行時に必須）
    prometheus_datasource_uid: str = ""
    loki_datasource_uid: str = ""

    promql_queries: list[str] = Field(default_factory=list)
    logql_queries: list[str] = Field(default_factory=list)
    target_instances: list[str] = Field(default_factory=list)
    time_range: TimeRange | None = None


class AgentState(MessagesState):
    """Multi-Agent ワークフローの共有ステート.

    Orchestrator → Metrics/Logs Agent → RCA Agent 間で共有される。

    Note: MessagesState は TypedDict ベースだが、LangGraph は内部的に
    デフォルト値付きフィールドをサポートしている。mypy はこれを
    "Right hand side values are not supported in TypedDict" と報告するため、
    デフォルト値を持つフィールドには type: ignore[misc] が必要。
    """

    investigation_id: str = ""  # type: ignore[misc]
    trigger_type: TriggerType = TriggerType.ALERT  # type: ignore[misc]
    alert: Alert | None = None  # type: ignore[misc]
    user_query: UserQuery | None = None  # type: ignore[misc]
    plan: InvestigationPlan | None = None  # type: ignore[misc]
    environment: EnvironmentContext | None = None  # type: ignore[misc]

    # Annotated + reducer を使うフィールドはデフォルト値不要のため ignore 不要
    metrics_results: Annotated[list[MetricsResult], _merge_list]
    logs_results: Annotated[list[LogsResult], _merge_list]

    rca_report: RCAReport | None = None  # type: ignore[misc]
    investigation_complete: bool = False  # type: ignore[misc]
    iteration_count: int = 0  # type: ignore[misc]
    max_iterations: int = 5  # type: ignore[misc]
    evaluation_feedback: EvaluationFeedback | None = None  # type: ignore[misc]
    pending_question: str = ""  # type: ignore[misc]
    user_response: str = ""  # type: ignore[misc]
