"""信頼度定量スコアリングのテスト."""

import pytest

from ai_agent_monitoring.core.confidence import (
    compute_anomaly_score,
    compute_confidence,
    compute_coverage_score,
    compute_evidence_score,
    compute_severity_score,
)
from ai_agent_monitoring.core.models import (
    KubernetesResourceState,
    KubernetesResult,
    LogsResult,
    MetricsResult,
    RootCause,
)


class TestComputeEvidenceScore:
    def test_no_evidence(self) -> None:
        rc = RootCause(description="test", confidence=0.5, evidence=[])
        assert compute_evidence_score(rc) == 0.0

    def test_one_evidence(self) -> None:
        rc = RootCause(description="test", confidence=0.5, evidence=["something happened"])
        assert compute_evidence_score(rc) == 0.4

    def test_three_evidence(self) -> None:
        rc = RootCause(
            description="test",
            confidence=0.5,
            evidence=["ev1", "ev2", "ev3"],
        )
        assert compute_evidence_score(rc) == 0.8

    def test_with_specific_value(self) -> None:
        rc = RootCause(
            description="test",
            confidence=0.5,
            evidence=["CPU usage reached 95%", "pod restarted"],
        )
        # base=0.6 (2件) + bonus=0.2 (具体値あり)
        assert compute_evidence_score(rc) == 0.8

    def test_three_with_specific_value_capped(self) -> None:
        rc = RootCause(
            description="test",
            confidence=0.5,
            evidence=["error 500ms latency", "ev2", "ev3"],
        )
        # base=0.8 + bonus=0.2 → capped at 1.0
        assert compute_evidence_score(rc) == 1.0


class TestComputeAnomalyScore:
    def test_zero_anomalies(self) -> None:
        assert compute_anomaly_score([], [], []) == 0.1

    def test_three_anomalies(self) -> None:
        mr = MetricsResult(query="q", anomalies=["a1", "a2"])
        lr = LogsResult(query="q", error_patterns=["e1"])
        assert compute_anomaly_score([mr], [lr], []) == 0.7

    def test_six_anomalies(self) -> None:
        mr = MetricsResult(query="q", anomalies=["a1", "a2", "a3"])
        kr = KubernetesResult(anomalies=["k1", "k2", "k3"])
        assert compute_anomaly_score([mr], [], [kr]) == 0.9


class TestComputeCoverageScore:
    def test_no_sources(self) -> None:
        assert compute_coverage_score([], [], []) == pytest.approx(0.0)

    def test_one_source(self) -> None:
        mr = MetricsResult(query="q")
        assert compute_coverage_score([mr], [], []) == pytest.approx(1 / 3)

    def test_two_sources(self) -> None:
        mr = MetricsResult(query="q")
        lr = LogsResult(query="q")
        assert compute_coverage_score([mr], [lr], []) == pytest.approx(2 / 3)

    def test_all_sources(self) -> None:
        mr = MetricsResult(query="q")
        lr = LogsResult(query="q")
        kr = KubernetesResult()
        assert compute_coverage_score([mr], [lr], [kr]) == pytest.approx(1.0)


class TestComputeSeverityScore:
    def test_no_k8s(self) -> None:
        assert compute_severity_score([]) == 0.5

    def test_crashloopbackoff(self) -> None:
        kr = KubernetesResult(
            resource_states=[
                KubernetesResourceState(kind="Pod", name="p1", status="CrashLoopBackOff"),
            ]
        )
        assert compute_severity_score([kr]) == 1.0

    def test_running_only(self) -> None:
        kr = KubernetesResult(
            resource_states=[
                KubernetesResourceState(kind="Pod", name="p1", status="Running"),
            ]
        )
        assert compute_severity_score([kr]) == 0.0


class TestComputeConfidence:
    def test_full_data(self) -> None:
        rc = RootCause(
            description="OOMKilled",
            confidence=0.8,
            evidence=["memory usage 95%", "pod restarted 3 times", "OOMKilled event"],
        )
        state = {
            "metrics_results": [MetricsResult(query="q", anomalies=["high_mem"])],
            "logs_results": [LogsResult(query="q", error_patterns=["OOM"])],
            "k8s_results": [
                KubernetesResult(
                    resource_states=[
                        KubernetesResourceState(kind="Pod", name="p1", status="OOMKilled"),
                    ],
                    anomalies=["OOMKilled"],
                )
            ],
        }
        details = compute_confidence(rc, state)
        assert 0.0 <= details.final_confidence <= 1.0
        assert details.coverage_score == pytest.approx(1.0)
        assert details.severity_score == 1.0
        assert details.evidence_score == 1.0  # 3件 + 具体値
        assert details.explanation  # 説明文が空でない

    def test_partial_data(self) -> None:
        rc = RootCause(description="high cpu", confidence=0.6, evidence=["cpu spike"])
        state = {
            "metrics_results": [MetricsResult(query="q", anomalies=["spike"])],
            "logs_results": [],
            "k8s_results": [],
        }
        details = compute_confidence(rc, state)
        assert 0.0 <= details.final_confidence <= 1.0
        assert details.coverage_score == pytest.approx(1 / 3)

    def test_no_data(self) -> None:
        rc = RootCause(description="unknown", confidence=0.3, evidence=[])
        state = {
            "metrics_results": [],
            "logs_results": [],
            "k8s_results": [],
        }
        details = compute_confidence(rc, state)
        assert 0.0 <= details.final_confidence <= 1.0
        assert details.coverage_score == 0.0
        assert details.evidence_score == 0.0


class TestLowCoverageAdjustment:
    def test_single_source_uses_higher_llm_ratio(self) -> None:
        """データソースが1つ以下の場合、LLM比率が60%に上がる."""
        rc = RootCause(description="test", confidence=0.9, evidence=["ev1"])
        state_low = {
            "metrics_results": [MetricsResult(query="q")],
            "logs_results": [],
            "k8s_results": [],
        }
        state_high = {
            "metrics_results": [MetricsResult(query="q")],
            "logs_results": [LogsResult(query="q")],
            "k8s_results": [],
        }
        details_low = compute_confidence(rc, state_low)
        details_high = compute_confidence(rc, state_high)
        # LLM confidence=0.9 が高いので、LLM比率が高い方が最終スコアも高くなる
        assert details_low.final_confidence > details_high.final_confidence
