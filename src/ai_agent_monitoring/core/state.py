"""LangGraph AgentState 定義."""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from ai_agent_monitoring.core.datasource import DatasourceInfo
from ai_agent_monitoring.core.models import (
    Alert,
    KubernetesResult,
    LogsResult,
    MetricsResult,
    RCAReport,
    TriggerType,
    UserQuery,
)


def _merge_list(left: list[Any], right: list[Any]) -> list[Any]:
    """リストをマージするreducer."""
    return left + right


def sanitize_tool_call_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """未応答の tool_calls に対してダミー ToolMessage を補完する.

    ReAct ループ上限で打ち切られた場合、最後の AIMessage に tool_calls が
    残るが対応する ToolMessage がない。OpenAI API はこれを拒否するため、
    ダミーの ToolMessage を挿入して整合性を保つ。
    """
    result: list[BaseMessage] = []
    for msg in messages:
        result.append(msg)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            # この AIMessage の直後にある ToolMessage の tool_call_id を収集
            answered_ids: set[str] = set()
            for following in messages[messages.index(msg) + 1 :]:
                if isinstance(following, ToolMessage):
                    answered_ids.add(following.tool_call_id)
                else:
                    break
            for tc in msg.tool_calls:
                if tc["id"] not in answered_ids:
                    result.append(
                        ToolMessage(
                            content="[ReActループ上限により実行されませんでした]",
                            tool_call_id=tc["id"],
                        )
                    )
    return result


_DEFAULT_MAX_TOOL_ERRORS_PER_NAME = 5


def count_tool_errors_by_name(messages: Sequence[BaseMessage]) -> dict[str, int]:
    """ToolMessage からツール名ごとのエラー回数を集計する."""
    counts: dict[str, int] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage) and getattr(msg, "status", None) == "error":
            name = getattr(msg, "name", None) or "unknown"
            counts[name] = counts.get(name, 0) + 1
    return counts


def should_stop_tool_loop(
    messages: Sequence[BaseMessage],
    max_react_steps: int,
    max_errors_per_tool: int = _DEFAULT_MAX_TOOL_ERRORS_PER_NAME,
) -> str | None:
    """ReAct ループを継続すべきか判定する.

    Returns:
        "tool_call" / "done" / None (最後のメッセージに tool_calls がない場合)
    """
    if not messages:
        return "done"
    last = messages[-1]
    if not (hasattr(last, "tool_calls") and last.tool_calls):
        return "done"

    tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
    if tool_msg_count >= max_react_steps:
        return "done"

    # 同じツールへのエラーが閾値を超えたら停止
    error_counts = count_tool_errors_by_name(messages)
    for _name, count in error_counts.items():
        if count >= max_errors_per_tool:
            return "done"

    return "tool_call"


_TOOL_OUTPUT_MAX_CHARS = 2000
_TOOL_OUTPUT_MAX_MESSAGES = 5


def extract_tool_outputs(messages: Sequence[BaseMessage]) -> list[str]:
    """ToolMessage からツール実行結果のテキストを抽出する.

    最新 _TOOL_OUTPUT_MAX_MESSAGES 件に制限し、各メッセージは最大 _TOOL_OUTPUT_MAX_CHARS 文字。
    """
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    # 最新N件
    recent = tool_msgs[-_TOOL_OUTPUT_MAX_MESSAGES:]
    return [str(m.content)[:_TOOL_OUTPUT_MAX_CHARS] for m in recent]


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

    # 全候補（エラーリカバリ・代替データソース情報用）
    prometheus_datasources: list[DatasourceInfo] = Field(default_factory=list)
    loki_datasources: list[DatasourceInfo] = Field(default_factory=list)

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

    model_config = {"extra": "ignore"}

    # データソースUID（クエリ実行時に必須）
    prometheus_datasource_uid: str = ""
    loki_datasource_uid: str = ""

    promql_queries: list[str] = Field(default_factory=list)
    logql_queries: list[str] = Field(default_factory=list)
    target_instances: list[str] = Field(default_factory=list)
    time_range: TimeRange | None = None

    # Kubernetes調査フィールド
    target_namespaces: list[str] = Field(default_factory=list)
    target_pods: list[str] = Field(default_factory=list)
    k8s_resource_kinds: list[str] = Field(default_factory=list)  # ["Deployment", "Service"] 等


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
    k8s_results: Annotated[list[KubernetesResult], _merge_list]

    rca_report: RCAReport | None = None  # type: ignore[misc]
    investigation_complete: bool = False  # type: ignore[misc]
    iteration_count: int = 0  # type: ignore[misc]
    max_iterations: int = 5  # type: ignore[misc]
    evaluation_feedback: EvaluationFeedback | None = None  # type: ignore[misc]
    pending_question: str = ""  # type: ignore[misc]
    user_response: str = ""  # type: ignore[misc]
