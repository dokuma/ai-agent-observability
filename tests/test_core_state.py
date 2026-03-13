"""core/state のテスト."""

from langchain_core.messages import AIMessage, ToolMessage

from ai_agent_monitoring.core.state import (
    AgentState,
    InvestigationPlan,
    TimeRange,
    count_tool_errors_by_name,
    extract_tool_outputs,
    should_stop_tool_loop,
)


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


class TestCountToolErrorsByName:
    def test_no_errors(self):
        messages = [
            ToolMessage(content="ok", tool_call_id="1", name="k8s_list_pods"),
        ]
        assert count_tool_errors_by_name(messages) == {}

    def test_counts_errors(self):
        messages = [
            ToolMessage(content="error1", tool_call_id="1", name="k8s_list_pods", status="error"),
            ToolMessage(content="ok", tool_call_id="2", name="k8s_list_pods"),
            ToolMessage(content="error2", tool_call_id="3", name="k8s_list_pods", status="error"),
            ToolMessage(content="error3", tool_call_id="4", name="k8s_get_pod", status="error"),
        ]
        counts = count_tool_errors_by_name(messages)
        assert counts["k8s_list_pods"] == 2
        assert counts["k8s_get_pod"] == 1


class TestShouldStopToolLoop:
    def test_no_tool_calls_returns_done(self):
        messages = [AIMessage(content="thinking")]
        assert should_stop_tool_loop(messages, max_react_steps=5) == "done"

    def test_with_tool_calls_returns_tool_call(self):
        ai = AIMessage(content="", tool_calls=[{"id": "1", "name": "test", "args": {}}])
        assert should_stop_tool_loop([ai], max_react_steps=5) == "tool_call"

    def test_max_steps_reached(self):
        ai = AIMessage(content="", tool_calls=[{"id": "2", "name": "test", "args": {}}])
        tools = [ToolMessage(content="ok", tool_call_id=str(i), name="test") for i in range(5)]
        assert should_stop_tool_loop([*tools, ai], max_react_steps=5) == "done"

    def test_too_many_errors_same_tool(self):
        ai = AIMessage(content="", tool_calls=[{"id": "x", "name": "test", "args": {}}])
        errors = [
            ToolMessage(content="err", tool_call_id=str(i), name="k8s_list_pods", status="error") for i in range(5)
        ]
        assert should_stop_tool_loop([*errors, ai], max_react_steps=20, max_errors_per_tool=5) == "done"

    def test_errors_below_threshold_continues(self):
        ai = AIMessage(content="", tool_calls=[{"id": "x", "name": "test", "args": {}}])
        errors = [
            ToolMessage(content="err", tool_call_id=str(i), name="k8s_list_pods", status="error") for i in range(3)
        ]
        assert should_stop_tool_loop([*errors, ai], max_react_steps=20, max_errors_per_tool=5) == "tool_call"


class TestExtractToolOutputs:
    def test_no_truncation(self):
        """ツール出力が切り詰められないことを確認."""
        long_text = "x" * 5000
        messages = [ToolMessage(content=long_text, tool_call_id="1")]
        result = extract_tool_outputs(messages)
        assert len(result) == 1
        assert len(result[0]) == 5000

    def test_max_messages_limit(self):
        """最新5件のみ抽出される."""
        messages = [ToolMessage(content=f"msg-{i}", tool_call_id=str(i)) for i in range(8)]
        result = extract_tool_outputs(messages)
        assert len(result) == 5
        assert result[0] == "msg-3"
        assert result[4] == "msg-7"


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
