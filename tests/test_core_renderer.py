"""renderer のテスト."""

from datetime import datetime

from ai_agent_monitoring.core.models import (
    ConfidenceDetails,
    RCAReport,
    RootCause,
    TriggerType,
)
from ai_agent_monitoring.core.renderer import render_rca_markdown


def test_render_with_confidence_details() -> None:
    """confidence_details がある場合、内訳テーブルがMarkdownに含まれること."""
    details = ConfidenceDetails(
        llm_confidence=0.80,
        evidence_score=0.60,
        anomaly_score=0.50,
        coverage_score=0.67,
        severity_score=0.50,
        quantitative_confidence=0.57,
        final_confidence=0.66,
        explanation="LLM判定=0.80(比率40%), 定量=0.57(比率60%) → 統合=0.66",
    )
    report = RCAReport(
        trigger_type=TriggerType.ALERT,
        root_causes=[
            RootCause(
                description="テスト原因",
                confidence=0.66,
                evidence=["エビデンス1"],
                confidence_details=details,
            )
        ],
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    md = render_rca_markdown(report)

    assert "信頼度スコア内訳" in md
    assert "| LLM判定 | 0.80 |" in md
    assert "| エビデンス充実度 | 0.60 |" in md
    assert "| 異常検出数 | 0.50 |" in md
    assert "| データカバレッジ | 0.67 |" in md
    assert "| K8s深刻度 | 0.50 |" in md
    assert "| **統合スコア** | **0.66** |" in md
    assert "<details>" in md
    assert "</details>" in md


def test_render_without_confidence_details() -> None:
    """confidence_details がない場合、内訳は表示されないこと."""
    report = RCAReport(
        trigger_type=TriggerType.USER_QUERY,
        root_causes=[RootCause(description="テスト原因", confidence=0.5, evidence=[])],
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    md = render_rca_markdown(report)

    assert "信頼度スコア内訳" not in md
    assert "50%" in md  # confidence bar の表示はある
