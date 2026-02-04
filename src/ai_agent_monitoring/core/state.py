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
    """

    # トリガー（どちらか一方が設定される）
    trigger_type: TriggerType = TriggerType.ALERT  # type: ignore[misc]
    alert: Alert | None = None  # type: ignore[misc]
    user_query: UserQuery | None = None  # type: ignore[misc]
    plan: InvestigationPlan | None = None  # type: ignore[misc]

    # 環境コンテキスト（利用可能なメトリクス・ラベル・ターゲット）
    environment: EnvironmentContext | None = None  # type: ignore[misc]

    # 各Agentの分析結果（リストでマージ）
    metrics_results: Annotated[list[MetricsResult], _merge_list]
    logs_results: Annotated[list[LogsResult], _merge_list]

    # 最終出力
    rca_report: RCAReport | None = None  # type: ignore[misc]

    # 制御フラグ
    investigation_complete: bool = False  # type: ignore[misc]
    iteration_count: int = 0  # type: ignore[misc]
    max_iterations: int = 5  # type: ignore[misc]

    # Human-in-the-loop: ユーザへの問い合わせ
    pending_question: str = ""  # type: ignore[misc]
    user_response: str = ""  # type: ignore[misc]
