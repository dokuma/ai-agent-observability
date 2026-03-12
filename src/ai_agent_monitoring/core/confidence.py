"""RCAレポート信頼度の定量スコアリング.

LLM判定と定量シグナルの加重平均でハイブリッドスコアを算出する。
純粋関数のみ。外部I/O依存なし。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ai_agent_monitoring.core.models import ConfidenceDetails

if TYPE_CHECKING:
    from ai_agent_monitoring.core.models import (
        KubernetesResult,
        LogsResult,
        MetricsResult,
        RootCause,
    )

# 定量サブスコアの重み
_EVIDENCE_WEIGHT = 0.35
_ANOMALY_WEIGHT = 0.30
_COVERAGE_WEIGHT = 0.20
_SEVERITY_WEIGHT = 0.15

# LLM vs 定量の比率（通常 / データ不足時）
_LLM_RATIO_NORMAL = 0.40
_LLM_RATIO_LOW_COVERAGE = 0.60

# K8sステータスの深刻度マッピング
_K8S_SEVERITY_MAP: dict[str, float] = {
    "CrashLoopBackOff": 1.0,
    "OOMKilled": 1.0,
    "Error": 0.9,
    "ImagePullBackOff": 0.8,
    "ErrImagePull": 0.8,
    "CreateContainerError": 0.8,
    "Pending": 0.5,
    "Terminating": 0.4,
    "Unknown": 0.3,
    "Running": 0.0,
    "Succeeded": 0.0,
    "Completed": 0.0,
}


def compute_evidence_score(root_cause: RootCause) -> float:
    """エビデンス件数と具体値含有に基づくスコア."""
    count = len(root_cause.evidence)
    if count == 0:
        base = 0.0
    elif count == 1:
        base = 0.4
    elif count == 2:
        base = 0.6
    else:
        base = 0.8

    # 具体値（数値・パーセント・単位）を含むエビデンスがあればボーナス
    has_specific = any(_contains_specific_value(ev) for ev in root_cause.evidence)
    bonus = 0.2 if has_specific else 0.0

    return min(1.0, base + bonus)


def _contains_specific_value(text: str) -> bool:
    """テキストに具体的な数値・メトリクス値が含まれるか判定."""
    import re

    return bool(re.search(r"\d+\.?\d*\s*(%|ms|s|MB|GB|Ki|Mi|Gi|m|cores?)", text))


def compute_anomaly_score(
    metrics_results: list[MetricsResult],
    logs_results: list[LogsResult],
    k8s_results: list[KubernetesResult],
) -> float:
    """異常検出数に基づくスコア."""
    total = 0
    for mr in metrics_results:
        total += len(mr.anomalies)
    for lr in logs_results:
        total += len(lr.error_patterns)
    for kr in k8s_results:
        total += len(kr.anomalies)

    if total == 0:
        return 0.1
    if total <= 2:
        return 0.5
    if total <= 5:
        return 0.7
    return 0.9


def compute_coverage_score(
    metrics_results: list[MetricsResult],
    logs_results: list[LogsResult],
    k8s_results: list[KubernetesResult],
) -> float:
    """データソースカバレッジ（metrics/logs/k8s のうち結果ありの割合）."""
    sources = 0
    if metrics_results:
        sources += 1
    if logs_results:
        sources += 1
    if k8s_results:
        sources += 1
    return sources / 3.0


def compute_severity_score(k8s_results: list[KubernetesResult]) -> float:
    """K8sリソース異常度に基づくスコア."""
    if not k8s_results:
        return 0.5  # 中立

    max_severity = 0.0
    for kr in k8s_results:
        for rs in kr.resource_states:
            severity = _K8S_SEVERITY_MAP.get(rs.status, 0.3)
            max_severity = max(max_severity, severity)

    return max_severity


def compute_confidence(root_cause: RootCause, state: dict[str, Any]) -> ConfidenceDetails:
    """定量スコアとLLM信頼度を統合してConfidenceDetailsを算出."""
    metrics_results = state.get("metrics_results", [])
    logs_results = state.get("logs_results", [])
    k8s_results = state.get("k8s_results", [])

    evidence = compute_evidence_score(root_cause)
    anomaly = compute_anomaly_score(metrics_results, logs_results, k8s_results)
    coverage = compute_coverage_score(metrics_results, logs_results, k8s_results)
    severity = compute_severity_score(k8s_results)

    # 定量サブスコアの加重平均
    quantitative = (
        evidence * _EVIDENCE_WEIGHT
        + anomaly * _ANOMALY_WEIGHT
        + coverage * _COVERAGE_WEIGHT
        + severity * _SEVERITY_WEIGHT
    )

    # データソース数に応じてLLM比率を調整
    source_count = sum(1 for s in [metrics_results, logs_results, k8s_results] if s)
    llm_ratio = _LLM_RATIO_LOW_COVERAGE if source_count <= 1 else _LLM_RATIO_NORMAL

    llm_conf = root_cause.confidence
    final = llm_conf * llm_ratio + quantitative * (1.0 - llm_ratio)

    explanation = (
        f"LLM判定={llm_conf:.2f}(比率{llm_ratio:.0%}), "
        f"定量={quantitative:.2f}(比率{1.0 - llm_ratio:.0%}) → "
        f"統合={final:.2f} "
        f"[evidence={evidence:.2f}, anomaly={anomaly:.2f}, "
        f"coverage={coverage:.2f}, severity={severity:.2f}]"
    )

    return ConfidenceDetails(
        llm_confidence=llm_conf,
        evidence_score=evidence,
        anomaly_score=anomaly,
        coverage_score=coverage,
        severity_score=severity,
        quantitative_confidence=round(quantitative, 4),
        final_confidence=round(final, 4),
        explanation=explanation,
    )
