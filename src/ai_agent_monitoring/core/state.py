"""LangGraph AgentState 定義."""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
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

logger = logging.getLogger(__name__)


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


_TOOL_OUTPUT_MAX_MESSAGES = 5


def _extract_text_from_content(content: Any) -> str:
    """ToolMessage.content からテキストコンテンツを抽出.

    LangChain ToolNode は dict や list 形式で content を格納することがある。
    {"content": [{"type": "text", "text": "..."}]} 形式から text を取り出す。
    str() による Python repr ではなく、元のテキストを返す。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        texts = []
        for item in content.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        if texts:
            return "\n".join(texts)
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        if texts:
            return "\n".join(texts)
    return str(content)


def extract_tool_outputs(messages: Sequence[BaseMessage]) -> list[str]:
    """ToolMessage からツール実行結果のテキストを抽出する.

    最新 _TOOL_OUTPUT_MAX_MESSAGES 件に制限。
    ツール結果はすでに _truncate_tool_result (8000文字) で制限されているため、
    ここでは追加の切り詰めを行わない。
    """
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    # 最新N件
    recent = tool_msgs[-_TOOL_OUTPUT_MAX_MESSAGES:]
    return [_extract_text_from_content(m.content) for m in recent]


_TOOL_OUTPUT_TRUNCATE_CHARS = 2000


def extract_tool_observations(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    """AIMessage.tool_calls と対応する ToolMessage をペアで抽出.

    各ツール実行の入力（ツール名+引数）と出力をペアにして返す。
    ReAct ループ内の全ツール実行を対象とする。

    Returns:
        [{"tool_name": "...", "tool_input": "...", "tool_output": "..."}, ...]
    """
    # tool_call_id → ToolMessage の内容マップ
    tool_msg_map: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            tool_msg_map[m.tool_call_id] = _extract_text_from_content(m.content)

    # AIMessage の tool_calls から入力を取得し、ToolMessage と紐付け
    observations: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, AIMessage) or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            tool_call_id = tc.get("id", "")
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            # 引数をクエリ/コマンドとして文字列化
            if isinstance(args, dict):
                # query, expr, namespace 等の主要引数を優先表示
                key_args = {k: v for k, v in args.items() if k not in ("datasourceUid",)}
                tool_input = ", ".join(f"{k}={v}" for k, v in key_args.items())
            else:
                tool_input = str(args)

            tool_output = tool_msg_map.get(tool_call_id or "", "")
            if tool_output:
                observations.append(
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "tool_output": tool_output[:_TOOL_OUTPUT_TRUNCATE_CHARS],
                    }
                )

    return observations


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


class K8sNamespaceSummary(BaseModel):
    """namespace ごとの K8s サマリ."""

    pod_count: int = 0
    pod_statuses: dict[str, int] = Field(default_factory=dict)
    warning_event_count: int = 0


class K8sEnvInfo(BaseModel):
    """K8s クラスタの環境情報."""

    namespaces: list[str] = Field(default_factory=list)
    namespace_summaries: dict[str, K8sNamespaceSummary] = Field(default_factory=dict)
    node_count: int = 0
    node_names: list[str] = Field(default_factory=list)


class PrometheusEnvInfo(BaseModel):
    """個別Prometheusデータソースの環境情報."""

    metrics: list[str] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)
    instances: list[str] = Field(default_factory=list)


class LokiEnvInfo(BaseModel):
    """個別Lokiデータソースの環境情報."""

    jobs: list[str] = Field(default_factory=list)


class EnvironmentContext(BaseModel):
    """監視環境のコンテキスト情報.

    Grafana MCPから取得した環境情報を格納する。
    調査計画の生成時に利用可能なメトリクス・ラベル・
    ターゲットを把握するために使用。
    """

    # データソース情報（複数DS対応）
    prometheus_datasource_uids: list[str] = Field(default_factory=list)
    loki_datasource_uids: list[str] = Field(default_factory=list)

    # 全候補（エラーリカバリ・代替データソース情報用）
    prometheus_datasources: list[DatasourceInfo] = Field(default_factory=list)
    loki_datasources: list[DatasourceInfo] = Field(default_factory=list)

    # DS別の環境情報
    prometheus_env_by_uid: dict[str, PrometheusEnvInfo] = Field(default_factory=dict)
    loki_env_by_uid: dict[str, LokiEnvInfo] = Field(default_factory=dict)

    # 利用可能なメトリクスとラベル値（全DSのマージ結果）
    available_metrics: list[str] = Field(default_factory=list)
    available_jobs: list[str] = Field(default_factory=list)
    available_instances: list[str] = Field(default_factory=list)

    # Lokiのjob情報
    loki_jobs: list[str] = Field(default_factory=list)

    # K8sクラスタ情報
    k8s_env: K8sEnvInfo = Field(default_factory=K8sEnvInfo)

    def merge_env_info(self) -> None:
        """DS別の環境情報をフラットフィールドにマージ."""
        if self.prometheus_env_by_uid:
            metrics: list[str] = []
            jobs: list[str] = []
            instances: list[str] = []
            seen_m: set[str] = set()
            seen_j: set[str] = set()
            seen_i: set[str] = set()
            for info in self.prometheus_env_by_uid.values():
                for v in info.metrics:
                    if v not in seen_m:
                        seen_m.add(v)
                        metrics.append(v)
                for v in info.jobs:
                    if v not in seen_j:
                        seen_j.add(v)
                        jobs.append(v)
                for v in info.instances:
                    if v not in seen_i:
                        seen_i.add(v)
                        instances.append(v)
            self.available_metrics = metrics
            self.available_jobs = jobs
            self.available_instances = instances

        if self.loki_env_by_uid:
            loki_jobs: list[str] = []
            seen_lj: set[str] = set()
            for loki_info in self.loki_env_by_uid.values():
                for v in loki_info.jobs:
                    if v not in seen_lj:
                        seen_lj.add(v)
                        loki_jobs.append(v)
            self.loki_jobs = loki_jobs

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
    reasoning: str = ""


class InvestigationPlanSchema(BaseModel):
    """LLM Structured Output 用スキーマ.

    LLM が生成すべきフィールドのみを含む。
    システム固有の値（datasource_uid, target_instances, target_namespaces,
    target_pods, time_range）は環境やアラート等のコンテキストから設定するため除外する。
    """

    promql_queries: list[str] = Field(default_factory=list)
    logql_queries: list[str] = Field(default_factory=list)
    k8s_resource_kinds: list[str] = Field(default_factory=list)  # ["Deployment", "Service"] 等


class InvestigationPlan(BaseModel):
    """Orchestratorが生成する調査計画."""

    model_config = {"extra": "ignore"}

    # データソースUID（クエリ実行時に必須、ユーザ選択値を強制設定）
    prometheus_datasource_uids: list[str] = Field(default_factory=list)
    loki_datasource_uids: list[str] = Field(default_factory=list)

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
