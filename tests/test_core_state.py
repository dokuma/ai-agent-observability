"""core/state のテスト."""

from ai_agent_monitoring.core.state import AgentState, InvestigationPlan, TimeRange


class TestTimeRange:
    def test_create(self, sample_time_range: TimeRange):
        assert sample_time_range.start < sample_time_range.end


class TestInvestigationPlan:
    def test_default_empty(self):
        plan = InvestigationPlan()
        assert plan.promql_queries == []
        assert plan.logql_queries == []
        assert plan.time_range is None

    def test_with_time_range(self, sample_plan: InvestigationPlan):
        assert sample_plan.time_range is not None
        assert len(sample_plan.promql_queries) == 2
        assert len(sample_plan.logql_queries) == 1


class TestAgentState:
    def test_default_schema(self):
        """AgentState のスキーマにすべてのフィールドが定義されていることを確認."""
        annotations = AgentState.__annotations__
        assert "trigger_type" in annotations
        assert "alert" in annotations
        assert "user_query" in annotations
        assert "metrics_results" in annotations
        assert "logs_results" in annotations
        assert "investigation_complete" in annotations
        assert "iteration_count" in annotations
        assert "max_iterations" in annotations
        assert "pending_question" in annotations
        assert "user_response" in annotations
