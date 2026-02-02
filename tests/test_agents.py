"""agents のテスト."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ai_agent_monitoring.agents.logs_agent import LogsAgent
from ai_agent_monitoring.agents.metrics_agent import MetricsAgent
from ai_agent_monitoring.agents.orchestrator import OrchestratorAgent
from ai_agent_monitoring.agents.rca_agent import RCAAgent
from ai_agent_monitoring.core.models import (
    LogEntry,
    LogsResult,
    RCAReport,
    RootCause,
    TriggerType,
    UserQuery,
)
from ai_agent_monitoring.core.state import AgentState, InvestigationPlan

# ---- ヘルパー ----

def _make_orchestrator():
    """テスト用OrchestratorAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    mock_mcp = MagicMock()
    mock_mcp.base_url = "http://mock:8080"
    mock_mcp.timeout = 30.0
    mock_mcp.call_tool = AsyncMock(return_value={})
    agent = OrchestratorAgent(
        llm=llm,
        prometheus_mcp=mock_mcp,
        loki_mcp=mock_mcp,
        grafana_mcp=mock_mcp,
    )
    return agent, llm


def _make_metrics_agent():
    """テスト用MetricsAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    mock_mcp = MagicMock()
    mock_mcp.base_url = "http://mock:8080"
    mock_mcp.timeout = 30.0
    mock_mcp.call_tool = AsyncMock(return_value={})
    agent = MetricsAgent(llm, mock_mcp, grafana_mcp=mock_mcp)
    return agent, llm


def _make_logs_agent():
    """テスト用LogsAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    mock_mcp = MagicMock()
    mock_mcp.base_url = "http://mock:8080"
    mock_mcp.timeout = 30.0
    mock_mcp.call_tool = AsyncMock(return_value={})
    agent = LogsAgent(llm, mock_mcp, grafana_mcp=mock_mcp)
    return agent, llm


def _make_rca_agent(with_grafana=False):
    """テスト用RCAAgentを生成."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock()
    grafana_mcp = None
    if with_grafana:
        grafana_mcp = MagicMock()
        grafana_mcp.base_url = "http://mock-grafana:3000"
        grafana_mcp.timeout = 30.0
        grafana_mcp.call_tool = AsyncMock(return_value={})
    agent = RCAAgent(llm, grafana_mcp=grafana_mcp, output_dir="/tmp/test_rca")
    return agent, llm


# ================================================================
# Orchestrator テスト
# ================================================================


class TestOrchestratorParsePlan:
    """Orchestrator の _parse_plan テスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    def test_parse_plan_json_block(self):
        content = (
            '```json\n{"promql_queries": ["up"], "logql_queries": [], "target_instances": [],'
            ' "time_range": {"start": "2026-02-01T15:00:00Z", "end": "2026-02-01T16:00:00Z"}}\n```'
        )
        plan = self.agent._parse_plan(content)

        assert plan.promql_queries == ["up"]
        assert plan.time_range is not None
        assert plan.time_range.start.year == 2026

    def test_parse_plan_raw_json(self):
        content = (
            'Here is the plan: {"promql_queries": ["rate(cpu[5m])"],'
            ' "logql_queries": ["{job=\\"app\\"}"], "target_instances": ["web-01"]}'
        )
        plan = self.agent._parse_plan(content)

        assert plan.promql_queries == ["rate(cpu[5m])"]
        assert plan.target_instances == ["web-01"]

    def test_parse_plan_invalid_json(self):
        content = "This is not valid JSON at all"
        plan = self.agent._parse_plan(content)

        # デフォルト計画が返る
        assert plan.promql_queries == []
        assert plan.time_range is None


class TestOrchestratorExtractJson:
    def test_extract_json_code_block(self):
        text = 'some text\n```json\n{"key": "value"}\n```\nmore text'
        result = OrchestratorAgent._extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_extract_json_inline(self):
        text = 'result is {"key": "value"} end'
        result = OrchestratorAgent._extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_extract_json_no_json(self):
        with pytest.raises(ValueError):
            OrchestratorAgent._extract_json("no json here")


class TestOrchestratorResolveTimeRange:
    """_resolve_time_range_node の時間範囲解決ロジックテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    def test_format_time_range_none(self):
        assert self.agent._format_time_range(None) == "指定なし"

    def test_format_time_range(self, sample_time_range):
        result = self.agent._format_time_range(sample_time_range)
        assert "2026-02-01" in result
        assert "〜" in result


class TestOrchestratorShouldContinue:
    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    def test_finish_when_complete(self):
        state = {"messages": [], "investigation_complete": True, "iteration_count": 0, "max_iterations": 5}
        assert self.agent._should_continue(state) == "finish"

    def test_finish_at_max_iterations(self):
        state = {"messages": [], "investigation_complete": False, "iteration_count": 5, "max_iterations": 5}
        assert self.agent._should_continue(state) == "finish"

    def test_continue_when_not_complete(self):
        state = {"messages": [], "investigation_complete": False, "iteration_count": 2, "max_iterations": 5}
        assert self.agent._should_continue(state) == "continue"


class TestOrchestratorAnalyzeInput:
    """_analyze_input ノードのテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    @pytest.mark.asyncio
    async def test_analyze_alert(self, sample_alert):
        response = MagicMock()
        response.content = "アラート分析結果"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
        )
        result = await self.agent._analyze_input(state)

        assert "messages" in result
        self.llm.ainvoke.assert_called_once()
        # 呼び出し引数にアラート情報が含まれる
        call_messages = self.llm.ainvoke.call_args[0][0]
        assert any("HighCPUUsage" in m.content for m in call_messages if isinstance(m, HumanMessage))

    @pytest.mark.asyncio
    async def test_analyze_user_query(self, sample_user_query):
        response = MagicMock()
        response.content = "ユーザクエリ分析結果"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=sample_user_query,
        )
        result = await self.agent._analyze_input(state)

        assert "messages" in result
        call_messages = self.llm.ainvoke.call_args[0][0]
        assert any("ユーザからの問い合わせ" in m.content for m in call_messages if isinstance(m, HumanMessage))

    @pytest.mark.asyncio
    async def test_analyze_invalid_input(self):
        response = MagicMock()
        response.content = "入力が不正"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], trigger_type=TriggerType.ALERT, alert=None)
        result = await self.agent._analyze_input(state)

        assert "messages" in result
        call_messages = self.llm.ainvoke.call_args[0][0]
        assert any("入力が不正" in m.content for m in call_messages if isinstance(m, HumanMessage))


class TestOrchestratorPlanInvestigation:
    """_plan_investigation ノードのテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    @pytest.mark.asyncio
    async def test_plan_investigation(self):
        response = MagicMock()
        response.content = json.dumps({
            "promql_queries": ["up"],
            "logql_queries": ['{job="app"}'],
            "target_instances": ["web-01"],
        })
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[AIMessage(content="分析結果")], iteration_count=0)
        result = await self.agent._plan_investigation(state)

        assert "plan" in result
        assert result["plan"].promql_queries == ["up"]
        assert result["iteration_count"] == 1


class TestOrchestratorResolveTimeRangeNode:
    """_resolve_time_range_node の非同期テスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    @pytest.mark.asyncio
    async def test_no_plan(self):
        state = AgentState(messages=[], plan=None)
        result = await self.agent._resolve_time_range_node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_plan_already_has_time_range(self, sample_plan):
        state = AgentState(messages=[], plan=sample_plan)
        result = await self.agent._resolve_time_range_node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_alert_auto_resolve(self, sample_alert):
        plan = InvestigationPlan(promql_queries=["up"], time_range=None)
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
            plan=plan,
        )
        result = await self.agent._resolve_time_range_node(state)

        assert "plan" in result
        assert result["plan"].time_range is not None
        # アラート時刻の30分前
        expected_start = sample_alert.starts_at - timedelta(minutes=30)
        assert result["plan"].time_range.start == expected_start

    @pytest.mark.asyncio
    async def test_user_query_with_time_range(self):
        start = datetime(2026, 2, 1, 16, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 17, 0, 0, tzinfo=timezone.utc)
        uq = UserQuery(raw_input="テスト", time_range_start=start, time_range_end=end)
        plan = InvestigationPlan(promql_queries=["up"], time_range=None)
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=uq,
            plan=plan,
        )
        result = await self.agent._resolve_time_range_node(state)
        assert result["plan"].time_range.start == start
        assert result["plan"].time_range.end == end

    @pytest.mark.asyncio
    async def test_user_query_start_only(self):
        start = datetime(2026, 2, 1, 16, 0, 0, tzinfo=timezone.utc)
        uq = UserQuery(raw_input="テスト", time_range_start=start)
        plan = InvestigationPlan(promql_queries=["up"], time_range=None)
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=uq,
            plan=plan,
        )
        result = await self.agent._resolve_time_range_node(state)
        assert result["plan"].time_range.start == start
        assert result["plan"].time_range.end == start + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_user_query_interrupt_success(self):
        """ユーザに時間範囲を問い合わせ、LLMがパースに成功するケース."""
        response = MagicMock()
        response.content = '{"start": "2026-02-01T16:00:00+00:00", "end": "2026-02-01T17:00:00+00:00"}'
        self.llm.ainvoke = AsyncMock(return_value=response)

        uq = UserQuery(raw_input="サーバの状態を確認して")
        plan = InvestigationPlan(promql_queries=["up"], time_range=None)
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=uq,
            plan=plan,
        )
        with patch("ai_agent_monitoring.agents.orchestrator.interrupt", return_value="昨日の16時から17時"):
            result = await self.agent._resolve_time_range_node(state)

        assert result["plan"].time_range is not None
        assert result["plan"].time_range.start.hour == 16

    @pytest.mark.asyncio
    async def test_user_query_interrupt_parse_fail_fallback(self):
        """LLMのパースに失敗した場合、直近1時間にフォールバック."""
        response = MagicMock()
        response.content = "パースできない内容"
        self.llm.ainvoke = AsyncMock(return_value=response)

        uq = UserQuery(raw_input="サーバの状態を確認して")
        plan = InvestigationPlan(promql_queries=["up"], time_range=None)
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=uq,
            plan=plan,
        )
        with patch("ai_agent_monitoring.agents.orchestrator.interrupt", return_value="わからない"):
            result = await self.agent._resolve_time_range_node(state)

        assert result["plan"].time_range is not None
        # フォールバック: 直近1時間なので end - start ≈ 1時間
        diff = result["plan"].time_range.end - result["plan"].time_range.start
        assert timedelta(minutes=59) <= diff <= timedelta(minutes=61)


class TestOrchestratorEvaluateResults:
    """_evaluate_results ノードのテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_orchestrator()

    @pytest.mark.asyncio
    async def test_sufficient(self, sample_metrics_result, sample_logs_result):
        response = MagicMock()
        response.content = "SUFFICIENT\n十分な情報があります。"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[AIMessage(content="prior")],
            metrics_results=[sample_metrics_result],
            logs_results=[sample_logs_result],
        )
        result = await self.agent._evaluate_results(state)

        assert result["investigation_complete"] is True

    @pytest.mark.asyncio
    async def test_insufficient(self):
        response = AIMessage(content="INSUFFICIENT\n追加調査が必要です。")
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[AIMessage(content="prior")],
            metrics_results=[],
            logs_results=[],
        )
        result = await self.agent._evaluate_results(state)

        assert result["investigation_complete"] is False

    @pytest.mark.asyncio
    async def test_no_results(self):
        response = MagicMock()
        response.content = "INSUFFICIENT\n結果がありません。"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[AIMessage(content="prior")])
        await self.agent._evaluate_results(state)

        # 結果なしでもLLMの判定に従う
        call_messages = self.llm.ainvoke.call_args[0][0]
        assert any("結果なし" in m.content for m in call_messages if isinstance(m, HumanMessage))


class TestOrchestratorCompile:
    def test_compile(self):
        agent, _ = _make_orchestrator()
        compiled = agent.compile()
        assert compiled is not None


# ================================================================
# Metrics Agent テスト
# ================================================================


class TestMetricsAgentReason:
    """MetricsAgent._reason のテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_metrics_agent()

    @pytest.mark.asyncio
    async def test_reason_no_plan(self):
        state = AgentState(messages=[], plan=None)
        result = await self.agent._reason(state)
        assert "調査計画がありません" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_reason_first_call(self, sample_plan):
        response = MagicMock(spec=AIMessage)
        response.content = "メトリクス分析開始"
        response.tool_calls = []
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], plan=sample_plan)
        result = await self.agent._reason(state)

        assert "messages" in result
        # 初回: システムプロンプトと調査指示が渡される
        call_messages = self.agent.llm.ainvoke.call_args[0][0]
        assert isinstance(call_messages[0], SystemMessage)
        assert "Metrics Agent" in call_messages[0].content

    @pytest.mark.asyncio
    async def test_reason_subsequent_call(self, sample_plan):
        response = MagicMock(spec=AIMessage)
        response.content = "続行"
        response.tool_calls = []
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        # すでにシステムプロンプトがあるメッセージ
        existing = [
            SystemMessage(content="あなたはMetrics Agentです。"),
            HumanMessage(content="クエリ実行してください"),
        ]
        state = AgentState(messages=existing, plan=sample_plan)
        await self.agent._reason(state)

        # 既存メッセージがそのまま渡される
        call_messages = self.agent.llm.ainvoke.call_args[0][0]
        assert call_messages == existing


class TestMetricsAgentSummarize:
    """MetricsAgent._summarize のテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_metrics_agent()

    @pytest.mark.asyncio
    async def test_summarize(self, sample_plan):
        response = MagicMock()
        response.content = "CPU使用率が異常に高い"
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[AIMessage(content="tool結果")], plan=sample_plan)
        result = await self.agent._summarize(state)

        assert "metrics_results" in result
        assert len(result["metrics_results"]) == 1
        assert result["metrics_results"][0].summary == "CPU使用率が異常に高い"
        assert "rate(node_cpu_seconds_total" in result["metrics_results"][0].query


class TestMetricsAgentShouldUseTool:
    def test_with_tool_calls(self):
        msg = MagicMock(spec=AIMessage)
        msg.tool_calls = [{"name": "query", "args": {}}]
        state = {"messages": [msg]}
        assert MetricsAgent._should_use_tool(state) == "tool_call"

    def test_without_tool_calls(self):
        msg = MagicMock(spec=AIMessage)
        msg.tool_calls = []
        state = {"messages": [msg]}
        assert MetricsAgent._should_use_tool(state) == "done"

    def test_plain_message(self):
        msg = MagicMock()
        del msg.tool_calls  # tool_callsがないメッセージ
        state = {"messages": [msg]}
        assert MetricsAgent._should_use_tool(state) == "done"


# ================================================================
# Logs Agent テスト
# ================================================================


class TestLogsAgentReason:
    """LogsAgent._reason のテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_logs_agent()

    @pytest.mark.asyncio
    async def test_reason_no_plan(self):
        state = AgentState(messages=[], plan=None)
        result = await self.agent._reason(state)
        assert "調査計画がありません" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_reason_first_call(self, sample_plan):
        response = MagicMock(spec=AIMessage)
        response.content = "ログ分析開始"
        response.tool_calls = []
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], plan=sample_plan)
        await self.agent._reason(state)

        call_messages = self.agent.llm.ainvoke.call_args[0][0]
        assert isinstance(call_messages[0], SystemMessage)
        assert "Logs Agent" in call_messages[0].content

    @pytest.mark.asyncio
    async def test_reason_with_time_range(self, sample_plan):
        response = MagicMock(spec=AIMessage)
        response.content = "時間範囲指定あり"
        response.tool_calls = []
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], plan=sample_plan)
        await self.agent._reason(state)

        call_messages = self.agent.llm.ainvoke.call_args[0][0]
        human_msg = [m for m in call_messages if isinstance(m, HumanMessage)][0]
        assert "2026-02-01" in human_msg.content


class TestLogsAgentSummarize:
    def setup_method(self):
        self.agent, self.llm = _make_logs_agent()

    @pytest.mark.asyncio
    async def test_summarize(self, sample_plan):
        response = MagicMock()
        response.content = "OOMエラーを検出"
        self.agent.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[AIMessage(content="tool結果")], plan=sample_plan)
        result = await self.agent._summarize(state)

        assert "logs_results" in result
        assert len(result["logs_results"]) == 1
        assert result["logs_results"][0].summary == "OOMエラーを検出"


class TestLogsAgentShouldUseTool:
    def test_with_tool_calls(self):
        msg = MagicMock(spec=AIMessage)
        msg.tool_calls = [{"name": "query", "args": {}}]
        state = {"messages": [msg]}
        assert LogsAgent._should_use_tool(state) == "tool_call"

    def test_without_tool_calls(self):
        msg = MagicMock(spec=AIMessage)
        msg.tool_calls = []
        state = {"messages": [msg]}
        assert LogsAgent._should_use_tool(state) == "done"


# ================================================================
# RCA Agent テスト
# ================================================================


class TestRCAAgentParseReport:
    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    def test_parse_valid_report(self, sample_alert):
        content = json.dumps({
            "root_causes": [
                {"description": "OOM", "confidence": 0.9, "evidence": ["heap full"]}
            ],
            "metrics_summary": "CPU high",
            "logs_summary": "OOM errors",
            "recommendations": ["increase memory"],
        })
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
        )
        report = self.agent._parse_report(content, state)

        assert len(report.root_causes) == 1
        assert report.root_causes[0].confidence == 0.9
        assert report.metrics_summary == "CPU high"
        assert report.recommendations == ["increase memory"]

    def test_parse_invalid_json_fallback(self, sample_alert):
        content = "This is not JSON, just a plain text description."
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
        )
        report = self.agent._parse_report(content, state)

        assert len(report.root_causes) == 1
        assert report.root_causes[0].confidence == 0.5
        assert report.root_causes[0].description == content


class TestRCAAgentExtractJson:
    def test_code_block(self):
        text = '```json\n{"key": "value"}\n```'
        result = RCAAgent._extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_inline(self):
        text = 'result: {"a": 1}'
        result = RCAAgent._extract_json(text)
        assert json.loads(result) == {"a": 1}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            RCAAgent._extract_json("no json")


class TestRCAAgentCorrelate:
    """_correlate ノードのテスト."""

    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_correlate_with_alert(self, sample_alert, sample_metrics_result, sample_logs_result):
        response = MagicMock()
        response.content = "相関分析結果"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
            metrics_results=[sample_metrics_result],
            logs_results=[sample_logs_result],
        )
        result = await self.agent._correlate(state)

        assert "messages" in result
        call_messages = self.llm.ainvoke.call_args[0][0]
        human = [m for m in call_messages if isinstance(m, HumanMessage)][0]
        assert "HighCPUUsage" in human.content
        assert "メトリクス分析結果" in human.content
        assert "ログ分析結果" in human.content

    @pytest.mark.asyncio
    async def test_correlate_with_user_query(self, sample_user_query):
        response = MagicMock()
        response.content = "相関分析結果"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=sample_user_query,
        )
        await self.agent._correlate(state)

        call_messages = self.llm.ainvoke.call_args[0][0]
        human = [m for m in call_messages if isinstance(m, HumanMessage)][0]
        assert "ユーザ問い合わせ" in human.content

    @pytest.mark.asyncio
    async def test_correlate_no_results(self):
        response = MagicMock()
        response.content = "データなし"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], trigger_type=TriggerType.ALERT)
        await self.agent._correlate(state)

        call_messages = self.llm.ainvoke.call_args[0][0]
        human = [m for m in call_messages if isinstance(m, HumanMessage)][0]
        assert "調査結果なし" in human.content


class TestRCAAgentReason:
    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_reason(self):
        response = MagicMock()
        response.content = "根本原因候補"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[AIMessage(content="相関分析結果")])
        result = await self.agent._reason(state)

        assert "messages" in result
        call_messages = self.llm.ainvoke.call_args[0][0]
        human = [m for m in call_messages if isinstance(m, HumanMessage)][0]
        assert "根本原因の候補" in human.content


class TestRCAAgentGenerateReport:
    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_generate_report(self, sample_alert):
        response = MagicMock()
        response.content = json.dumps({
            "root_causes": [{"description": "OOM", "confidence": 0.85, "evidence": ["heap"]}],
            "metrics_summary": "CPU高",
            "logs_summary": "OOMエラー",
            "recommendations": ["メモリ増設"],
        })
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(
            messages=[AIMessage(content="推論結果")],
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
        )
        result = await self.agent._generate_report(state)

        assert "rca_report" in result
        report = result["rca_report"]
        assert isinstance(report, RCAReport)
        assert report.root_causes[0].confidence == 0.85


class TestRCAAgentCollectEvidence:
    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_no_report(self):
        state = AgentState(messages=[], rca_report=None)
        result = await self.agent._collect_evidence(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_grafana(self, sample_metrics_result, sample_logs_result):
        """Grafana未設定時はスナップショットなし、ログ抜粋のみ."""
        report = RCAReport(trigger_type=TriggerType.ALERT)
        state = AgentState(
            messages=[],
            rca_report=report,
            metrics_results=[sample_metrics_result],
            logs_results=[sample_logs_result],
        )
        result = await self.agent._collect_evidence(state)

        assert result["rca_report"].panel_snapshots == []
        assert len(result["rca_report"].log_excerpts) == 1
        assert result["rca_report"].log_excerpts[0].caption.startswith("ログ抜粋")


class TestRCAAgentCollectLogExcerpts:
    def setup_method(self):
        self.agent, _ = _make_rca_agent()

    def test_collect_excerpts(self, sample_logs_result):
        state = AgentState(messages=[], logs_results=[sample_logs_result])
        excerpts = self.agent._collect_log_excerpts(state)

        assert len(excerpts) == 1
        assert excerpts[0].query == '{job="myapp"} |= "error"'
        assert len(excerpts[0].entries) == 2

    def test_empty_results(self):
        state = AgentState(messages=[])
        excerpts = self.agent._collect_log_excerpts(state)
        assert excerpts == []

    def test_limit_20_entries(self):
        """20件を超えるエントリは切り捨て."""
        entries = [
            LogEntry(
                timestamp=datetime(2026, 2, 1, 16, 0, i, tzinfo=timezone.utc),
                level="error",
                message=f"error {i}",
            )
            for i in range(30)
        ]
        lr = LogsResult(query="test", entries=entries)
        state = AgentState(messages=[], logs_results=[lr])
        excerpts = self.agent._collect_log_excerpts(state)

        assert len(excerpts[0].entries) == 20


class TestRCAAgentCaptureSnapshots:
    @pytest.mark.asyncio
    async def test_no_grafana(self):
        agent, _ = _make_rca_agent(with_grafana=False)
        state = AgentState(messages=[], metrics_results=[])
        result = await agent._capture_panel_snapshots(state)
        assert result == []

    @pytest.mark.asyncio
    async def test_with_grafana(self, sample_metrics_result, sample_plan, tmp_path):
        agent, _ = _make_rca_agent(with_grafana=True)
        agent.output_dir = tmp_path

        # Grafanaメソッドをモック
        agent.grafana.search_dashboards = AsyncMock(return_value={
            "dashboards": [{"uid": "dash1"}],
        })
        agent.grafana.get_dashboard_panels = AsyncMock(return_value={
            "panels": [{"id": 1}],
        })
        agent.grafana.render_panel_image = AsyncMock(return_value=b"\x89PNG fake")

        state = AgentState(
            messages=[],
            metrics_results=[sample_metrics_result],
            plan=sample_plan,
        )
        snapshots = await agent._capture_panel_snapshots(state)

        assert len(snapshots) == 1
        assert snapshots[0].dashboard_uid == "dash1"
        assert snapshots[0].panel_id == 1

    @pytest.mark.asyncio
    async def test_grafana_error_handled(self, sample_metrics_result):
        """Grafanaエラー時もクラッシュせず空リスト."""
        agent, _ = _make_rca_agent(with_grafana=True)
        agent.grafana.search_dashboards = AsyncMock(side_effect=Exception("connection error"))

        state = AgentState(messages=[], metrics_results=[sample_metrics_result])
        snapshots = await agent._capture_panel_snapshots(state)
        assert snapshots == []


class TestRCAAgentRenderMarkdown:
    def setup_method(self):
        self.agent, _ = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_no_report(self):
        state = AgentState(messages=[], rca_report=None)
        result = await self.agent._render_markdown(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_render_and_save(self, tmp_path):
        self.agent.output_dir = tmp_path
        report = RCAReport(
            trigger_type=TriggerType.ALERT,
            root_causes=[RootCause(description="test", confidence=0.8)],
            markdown="",
        )
        state = AgentState(messages=[], rca_report=report)

        result = await self.agent._render_markdown(state)

        assert result["rca_report"].markdown != ""
        assert "# RCA レポート" in result["rca_report"].markdown
        # ファイルが実際に保存されたか確認
        md_files = list(tmp_path.glob("rca_report_*.md"))
        assert len(md_files) == 1
