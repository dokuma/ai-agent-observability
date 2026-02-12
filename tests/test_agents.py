"""agents のテスト."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
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
from ai_agent_monitoring.core.state import (
    AgentState,
    DashboardInfo,
    EnvironmentContext,
    InvestigationPlan,
)
from ai_agent_monitoring.tools.registry import MCPConnection, ToolRegistry

# ---- ヘルパー ----


def _make_mock_mcp():
    """モックMCPクライアントを生成."""
    mock_mcp = MagicMock()
    mock_mcp.base_url = "http://mock:8080"
    mock_mcp.timeout = 30.0
    mock_mcp.call_tool = AsyncMock(return_value={})
    return mock_mcp


def _make_mock_registry():
    """モックToolRegistryを生成（全て健全）."""
    mock_mcp = _make_mock_mcp()
    registry = MagicMock(spec=ToolRegistry)
    registry.prometheus = MCPConnection(name="prometheus", client=mock_mcp, healthy=True)
    registry.loki = MCPConnection(name="loki", client=mock_mcp, healthy=True)
    registry.grafana = MCPConnection(name="grafana", client=mock_mcp, healthy=True)
    return registry


def _make_orchestrator():
    """テスト用OrchestratorAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    registry = _make_mock_registry()
    agent = OrchestratorAgent(
        llm=llm,
        registry=registry,
    )
    return agent, llm


def _make_metrics_agent():
    """テスト用MetricsAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    mock_mcp = _make_mock_mcp()
    agent = MetricsAgent(llm, prometheus_mcp=mock_mcp, grafana_mcp=mock_mcp)
    return agent, llm


def _make_logs_agent():
    """テスト用LogsAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    mock_mcp = _make_mock_mcp()
    agent = LogsAgent(llm, loki_mcp=mock_mcp, grafana_mcp=mock_mcp)
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

        # パース失敗時は例外が発生する（デフォルト計画にフォールバックしない）
        with pytest.raises(ValueError, match="調査計画のパースに失敗しました"):
            self.agent._parse_plan(content)

    def test_parse_plan_time_range_as_string(self):
        """time_rangeが文字列の場合はNoneに正規化."""
        content = '{"promql_queries": ["up"], "logql_queries": [], "time_range": "2026-02-05T07:11:18+00:00"}'
        plan = self.agent._parse_plan(content)

        assert plan.promql_queries == ["up"]
        # 文字列のtime_rangeはNoneに変換される
        assert plan.time_range is None

    def test_parse_plan_time_range_invalid_dict(self):
        """time_rangeがstart/endを持たないdictの場合はNoneに正規化."""
        content = '{"promql_queries": ["up"], "logql_queries": [], "time_range": {"invalid": "value"}}'
        plan = self.agent._parse_plan(content)

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
        response.content = json.dumps(
            {
                "promql_queries": ["up"],
                "logql_queries": ['{job="app"}'],
                "target_instances": ["web-01"],
            }
        )
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
        start = datetime(2026, 2, 1, 16, 0, 0, tzinfo=UTC)
        end = datetime(2026, 2, 1, 17, 0, 0, tzinfo=UTC)
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
        start = datetime(2026, 2, 1, 16, 0, 0, tzinfo=UTC)
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


class TestOrchestratorStageUpdate:
    """Orchestrator のステージ更新機能のテスト."""

    def test_update_stage_with_callback(self):
        """コールバックが設定されている場合にステージが更新される."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = _make_mock_registry()

        # コールバックをモック
        callback = MagicMock()
        agent = OrchestratorAgent(
            llm=llm,
            registry=registry,
            stage_update_callback=callback,
        )

        state = AgentState(
            messages=[],
            investigation_id="test-inv-123",
            iteration_count=2,
        )

        agent._update_stage(state, "テストステージ")

        callback.assert_called_once_with("test-inv-123", "テストステージ", 2)

    def test_update_stage_no_callback(self):
        """コールバックが未設定の場合は何もしない."""
        agent, _ = _make_orchestrator()
        state = AgentState(
            messages=[],
            investigation_id="test-inv-123",
            iteration_count=1,
        )

        # 例外が発生しないことを確認
        agent._update_stage(state, "テストステージ")

    def test_update_stage_no_investigation_id(self):
        """investigation_idが空の場合はコールバックを呼ばない."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = _make_mock_registry()

        callback = MagicMock()
        agent = OrchestratorAgent(
            llm=llm,
            registry=registry,
            stage_update_callback=callback,
        )

        state = AgentState(
            messages=[],
            investigation_id="",  # 空
            iteration_count=1,
        )

        agent._update_stage(state, "テストステージ")

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrap_with_stage(self):
        """_wrap_with_stageがサブグラフ実行前にステージを更新する."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = _make_mock_registry()

        callback = MagicMock()
        agent = OrchestratorAgent(
            llm=llm,
            registry=registry,
            stage_update_callback=callback,
        )

        # モックのサブグラフ
        mock_subgraph = MagicMock()
        mock_subgraph.ainvoke = AsyncMock(return_value={"test_result": "ok"})

        wrapped = agent._wrap_with_stage(mock_subgraph, "ラップテスト")

        state = AgentState(
            messages=[],
            investigation_id="test-inv-456",
            iteration_count=0,
        )

        config: dict[str, Any] = {"callbacks": []}
        result = await wrapped(state, config)

        # コールバックが呼ばれた
        callback.assert_called_once_with("test-inv-456", "ラップテスト", 0)
        # サブグラフが実行された（config伝播あり）
        mock_subgraph.ainvoke.assert_called_once_with(state, config=config)
        # 結果が返された
        assert result == {"test_result": "ok"}

    @pytest.mark.asyncio
    async def test_discover_environment_updates_stage(self):
        """_discover_environmentがステージを更新する."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = _make_mock_registry()

        callback = MagicMock()
        agent = OrchestratorAgent(
            llm=llm,
            registry=registry,
            stage_update_callback=callback,
        )

        state = AgentState(
            messages=[],
            investigation_id="test-inv-789",
            iteration_count=0,
        )

        # grafana_toolをNoneに設定してスキップ
        agent.grafana_tool = None

        await agent._discover_environment(state)

        callback.assert_called_with("test-inv-789", "環境情報を収集中", 0)


class TestOrchestratorEnvironmentDiscovery:
    """Orchestrator の環境発見機能のテスト."""

    @pytest.mark.asyncio
    async def test_discover_environment_no_grafana(self):
        """Grafana MCPがない場合は空のコンテキストを返す."""
        agent, _ = _make_orchestrator()
        agent.grafana_tool = None

        state = AgentState(messages=[])
        result = await agent._discover_environment(state)

        assert "environment" in result
        env = result["environment"]
        assert env.prometheus_datasource_uid == ""
        assert env.loki_datasource_uid == ""
        assert env.available_metrics == []

    def test_extract_content_text(self):
        """MCPツール結果からテキストを抽出."""
        agent, _ = _make_orchestrator()

        result = {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ]
        }
        text = agent._extract_content_text(result)
        assert text == "hello\nworld"

    def test_extract_content_text_empty(self):
        """空の結果から抽出."""
        agent, _ = _make_orchestrator()

        result = {"content": []}
        text = agent._extract_content_text(result)
        assert text == ""

    def test_extract_list_from_result_json(self):
        """JSONリストを抽出."""
        agent, _ = _make_orchestrator()

        result = {"content": [{"type": "text", "text": '["metric1", "metric2", "metric3"]'}]}
        items = agent._extract_list_from_result(result)
        assert items == ["metric1", "metric2", "metric3"]

    def test_extract_list_from_result_newlines(self):
        """改行区切りリストを抽出."""
        agent, _ = _make_orchestrator()

        result = {"content": [{"type": "text", "text": "item1\nitem2\nitem3"}]}
        items = agent._extract_list_from_result(result)
        assert items == ["item1", "item2", "item3"]

    def test_parse_datasources_valid(self):
        """有効なデータソーステキストをパース."""
        agent, _ = _make_orchestrator()

        text = '[{"type": "prometheus", "uid": "prom-123"}, {"type": "loki", "uid": "loki-456"}]'
        result = agent._parse_datasources(text)
        assert len(result) == 2
        assert result[0]["type"] == "prometheus"
        assert result[1]["uid"] == "loki-456"

    def test_parse_datasources_invalid(self):
        """無効なテキストは空リストを返す."""
        agent, _ = _make_orchestrator()

        text = "not valid json"
        result = agent._parse_datasources(text)
        assert result == []

    def test_parse_dashboards_valid(self):
        """有効なダッシュボードテキストをパース."""
        agent, _ = _make_orchestrator()

        text = '[{"uid": "dash-1", "title": "Test"}, {"uid": "dash-2", "title": "Test2"}]'
        result = agent._parse_dashboards(text)
        assert len(result) == 2
        assert result[0]["uid"] == "dash-1"

    def test_parse_dashboards_invalid(self):
        """無効なテキストは空リストを返す."""
        agent, _ = _make_orchestrator()

        text = "invalid"
        result = agent._parse_dashboards(text)
        assert result == []

    def test_extract_queries_from_panels_promql(self):
        """パネルからPromQLクエリを抽出."""
        agent, _ = _make_orchestrator()

        text = '[{"expr": "rate(http_requests_total[5m])"}, {"query": "node_cpu_seconds_total"}]'
        promql, logql = agent._extract_queries_from_panels(text)
        assert len(promql) == 2
        assert "rate(http_requests_total[5m])" in promql
        assert "node_cpu_seconds_total" in promql
        assert len(logql) == 0

    def test_extract_queries_from_panels_logql(self):
        """パネルからLogQLクエリを抽出."""
        agent, _ = _make_orchestrator()

        text = '[{"expr": "{job=\\"app\\"} |= \\"error\\""}, {"expr": "up"}]'
        promql, logql = agent._extract_queries_from_panels(text)
        assert len(logql) == 1
        assert len(promql) == 1

    def test_extract_queries_from_panels_invalid(self):
        """無効なJSONは空リストを返す."""
        agent, _ = _make_orchestrator()

        text = "not json"
        promql, logql = agent._extract_queries_from_panels(text)
        assert promql == []
        assert logql == []


class TestOrchestratorFormatEnvironmentContext:
    """Orchestrator の _format_environment_context テスト."""

    def test_none_environment(self):
        """Noneの場合はメッセージを返す."""
        agent, _ = _make_orchestrator()
        result = agent._format_environment_context(None)
        assert "環境情報は利用できません" in result

    def test_with_prometheus_datasource(self):
        """Prometheusデータソースが含まれる."""
        from ai_agent_monitoring.core.state import EnvironmentContext

        agent, _ = _make_orchestrator()
        env = EnvironmentContext(prometheus_datasource_uid="prom-uid-123")
        result = agent._format_environment_context(env)
        assert "prom-uid-123" in result

    def test_with_metrics(self):
        """メトリクスが含まれる."""
        from ai_agent_monitoring.core.state import EnvironmentContext

        agent, _ = _make_orchestrator()
        env = EnvironmentContext(available_metrics=["cpu_usage", "memory_usage", "disk_io"])
        result = agent._format_environment_context(env)
        assert "cpu_usage" in result
        assert "利用可能なPrometheusメトリクス" in result

    def test_with_jobs(self):
        """ジョブが含まれる."""
        from ai_agent_monitoring.core.state import EnvironmentContext

        agent, _ = _make_orchestrator()
        env = EnvironmentContext(available_jobs=["node_exporter", "prometheus"])
        result = agent._format_environment_context(env)
        assert "node_exporter" in result
        assert "jobラベル値" in result


class TestOrchestratorValidateQueries:
    """Orchestrator の _validate_queries テスト."""

    @pytest.mark.asyncio
    async def test_no_plan(self):
        """プランがない場合は何もしない."""
        agent, _ = _make_orchestrator()
        state = AgentState(messages=[], plan=None)
        result = await agent._validate_queries(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_valid_promql_queries(self):
        """有効なPromQLクエリはそのまま通過."""
        agent, _ = _make_orchestrator()
        plan = InvestigationPlan(
            promql_queries=["up", "rate(http_requests_total[5m])"],
            logql_queries=[],
        )
        state = AgentState(messages=[], plan=plan)

        result = await agent._validate_queries(state)

        # 有効なクエリは維持される
        assert result["plan"].promql_queries == ["up", "rate(http_requests_total[5m])"]

    @pytest.mark.asyncio
    async def test_valid_logql_queries(self):
        """有効なLogQLクエリはそのまま通過."""
        agent, _ = _make_orchestrator()
        plan = InvestigationPlan(
            promql_queries=[],
            logql_queries=['{job="app"}', '{job="nginx"} |= "error"'],
        )
        state = AgentState(messages=[], plan=plan)

        result = await agent._validate_queries(state)

        assert len(result["plan"].logql_queries) == 2

    @pytest.mark.asyncio
    async def test_auto_correct_promql(self):
        """自動修正可能なPromQLクエリは修正される."""
        agent, llm = _make_orchestrator()

        # LLMの応答を設定（再生成時に呼ばれる）
        response = MagicMock()
        response.content = '{"promql_queries": ["rate(http_requests_total[5m])"], "logql_queries": []}'
        llm.ainvoke = AsyncMock(return_value=response)

        # 閉じ括弧が足りないクエリ
        plan = InvestigationPlan(
            promql_queries=["rate(http_requests_total[5m]"],  # 閉じ括弧不足
            logql_queries=[],
        )
        state = AgentState(messages=[], plan=plan)

        result = await agent._validate_queries(state)

        # 自動修正されるか、LLMで再生成される
        assert "plan" in result

    @pytest.mark.asyncio
    async def test_invalid_query_triggers_llm_retry(self):
        """無効なクエリがある場合、LLMに再生成を依頼."""
        agent, llm = _make_orchestrator()

        # LLMの応答を設定
        response = MagicMock()
        response.content = '{"promql_queries": ["up"], "logql_queries": []}'
        llm.ainvoke = AsyncMock(return_value=response)

        # 完全に無効なクエリ
        plan = InvestigationPlan(
            promql_queries=["invalid query {{{{"],
            logql_queries=[],
        )
        state = AgentState(messages=[], plan=plan)

        await agent._validate_queries(state)

        # LLMが呼ばれた
        assert llm.ainvoke.called


class TestOrchestratorGetRagContext:
    """Orchestrator の _get_rag_context テスト."""

    def test_empty_query(self):
        """空のクエリは空文字を返す."""
        agent, _ = _make_orchestrator()
        result = agent._get_rag_context("")
        assert result == ""

    def test_with_query(self):
        """クエリがある場合はRAGを検索."""
        agent, _ = _make_orchestrator()
        # RAGが設定されていない環境では空または例外をキャッチ
        result = agent._get_rag_context("CPU usage high")
        # 結果は文字列（空でもOK）
        assert isinstance(result, str)


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
        human_msg = next(m for m in call_messages if isinstance(m, HumanMessage))
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
        content = json.dumps(
            {
                "root_causes": [{"description": "OOM", "confidence": 0.9, "evidence": ["heap full"]}],
                "metrics_summary": "CPU high",
                "logs_summary": "OOM errors",
                "recommendations": ["increase memory"],
            }
        )
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
        human = next(m for m in call_messages if isinstance(m, HumanMessage))
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
        human = next(m for m in call_messages if isinstance(m, HumanMessage))
        assert "ユーザ問い合わせ" in human.content

    @pytest.mark.asyncio
    async def test_correlate_no_results(self):
        response = MagicMock()
        response.content = "データなし"
        self.llm.ainvoke = AsyncMock(return_value=response)

        state = AgentState(messages=[], trigger_type=TriggerType.ALERT)
        await self.agent._correlate(state)

        call_messages = self.llm.ainvoke.call_args[0][0]
        human = next(m for m in call_messages if isinstance(m, HumanMessage))
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
        human = next(m for m in call_messages if isinstance(m, HumanMessage))
        assert "根本原因の候補" in human.content


class TestRCAAgentGenerateReport:
    def setup_method(self):
        self.agent, self.llm = _make_rca_agent()

    @pytest.mark.asyncio
    async def test_generate_report(self, sample_alert):
        response = MagicMock()
        response.content = json.dumps(
            {
                "root_causes": [{"description": "OOM", "confidence": 0.85, "evidence": ["heap"]}],
                "metrics_summary": "CPU高",
                "logs_summary": "OOMエラー",
                "recommendations": ["メモリ増設"],
            }
        )
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
                timestamp=datetime(2026, 2, 1, 16, 0, i, tzinfo=UTC),
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
        agent.grafana.search_dashboards = AsyncMock(
            return_value={
                "dashboards": [{"uid": "dash1"}],
            }
        )
        agent.grafana.get_dashboard_panels = AsyncMock(
            return_value={
                "panels": [{"id": 1}],
            }
        )
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


# ---- ダッシュボード選択戦略テスト ----


class TestOrchestratorExtractInvestigationKeywords:
    """キーワード抽出のテスト."""

    def setup_method(self):
        self.agent, _ = _make_orchestrator()

    def test_extract_cpu_keywords(self):
        """CPUキーワードの抽出."""
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=UserQuery(raw_input="CPUの使用率を調べてください"),
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert "cpu" in keywords
        assert "usage" in keywords or "utilization" in keywords

    def test_extract_memory_keywords(self):
        """メモリキーワードの抽出."""
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=UserQuery(raw_input="メモリ不足の原因を調査"),
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert "memory" in keywords or "mem" in keywords or "ram" in keywords

    def test_extract_network_keywords(self):
        """ネットワークキーワードの抽出."""
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=UserQuery(raw_input="ネットワーク遅延の調査"),
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert "network" in keywords or "net" in keywords

    def test_extract_kubernetes_keywords(self):
        """Kubernetesキーワードの抽出."""
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=UserQuery(raw_input="Kubernetes podのエラーを調査"),
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert "kubernetes" in keywords or "pod" in keywords

    def test_extract_from_alert(self):
        """アラートからのキーワード抽出."""
        from ai_agent_monitoring.core.models import Alert

        state = AgentState(
            messages=[],
            trigger_type=TriggerType.ALERT,
            alert=Alert(
                alert_name="HighCPUUsage",
                severity="critical",
                instance="server1",
                summary="CPU usage above 90%",
                description="High CPU detected on production server",
                starts_at=datetime.now(UTC),
            ),
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert "cpu" in keywords
        assert "server" in keywords or "production" in keywords

    def test_empty_query(self):
        """空のクエリ."""
        state = AgentState(
            messages=[],
            trigger_type=TriggerType.USER_QUERY,
            user_query=None,
        )
        keywords = self.agent._extract_investigation_keywords(state)
        assert keywords == []


class TestOrchestratorDashboardScoring:
    """ダッシュボードスコアリングのテスト."""

    def setup_method(self):
        self.agent, _ = _make_orchestrator()

    def test_score_title_match(self):
        """タイトルマッチでスコア加算."""
        dashboard = DashboardInfo(uid="1", title="Node CPU Usage", tags=[])
        score = self.agent._score_dashboard_relevance(dashboard, ["cpu"])
        assert score > 0

    def test_score_tag_match(self):
        """タグマッチでスコア加算."""
        dashboard = DashboardInfo(uid="1", title="Overview", tags=["cpu", "memory"])
        score = self.agent._score_dashboard_relevance(dashboard, ["cpu"])
        assert score > 0

    def test_score_no_match(self):
        """マッチなしでスコア0."""
        dashboard = DashboardInfo(uid="1", title="Network Traffic", tags=["network"])
        score = self.agent._score_dashboard_relevance(dashboard, ["cpu"])
        assert score == 0

    def test_score_empty_keywords(self):
        """キーワード空でスコア0."""
        dashboard = DashboardInfo(uid="1", title="CPU Usage", tags=[])
        score = self.agent._score_dashboard_relevance(dashboard, [])
        assert score == 0

    def test_title_scores_higher_than_tag(self):
        """タイトルマッチはタグマッチより高スコア."""
        db_title_match = DashboardInfo(uid="1", title="CPU Usage", tags=[])
        db_tag_match = DashboardInfo(uid="2", title="Overview", tags=["cpu"])

        title_score = self.agent._score_dashboard_relevance(db_title_match, ["cpu"])
        tag_score = self.agent._score_dashboard_relevance(db_tag_match, ["cpu"])

        assert title_score > tag_score


class TestOrchestratorDashboardRanking:
    """ダッシュボードランキングのテスト."""

    def setup_method(self):
        self.agent, _ = _make_orchestrator()

    def test_rank_by_relevance(self):
        """関連度順にソート."""
        dashboards = [
            DashboardInfo(uid="1", title="Overview", tags=[]),
            DashboardInfo(uid="2", title="CPU Usage Dashboard", tags=["cpu"]),
            DashboardInfo(uid="3", title="Network", tags=["cpu"]),
        ]
        ranked = self.agent._rank_dashboards_by_keywords(dashboards, ["cpu"])

        # CPUがタイトルに含まれるものが最上位
        assert ranked[0].uid == "2"
        # スコア降順
        assert ranked[0].relevance_score >= ranked[1].relevance_score

    def test_rank_empty_keywords(self):
        """キーワード空でも全ダッシュボード含む."""
        dashboards = [
            DashboardInfo(uid="1", title="A"),
            DashboardInfo(uid="2", title="B"),
        ]
        ranked = self.agent._rank_dashboards_by_keywords(dashboards, [])
        assert len(ranked) == 2
        # 全てスコア0
        assert all(d.relevance_score == 0 for d in ranked)


class TestOrchestratorParsePanelQueries:
    """パネルクエリパースのテスト."""

    def setup_method(self):
        self.agent, _ = _make_orchestrator()

    def test_parse_promql(self):
        """PromQLクエリのパース."""
        dashboard = DashboardInfo(uid="dash1", title="Test Dashboard")
        text = json.dumps(
            [
                {"title": "CPU Panel", "expr": "rate(node_cpu_seconds_total[5m])"},
            ]
        )
        queries = self.agent._parse_panel_queries(text, dashboard)

        assert len(queries) == 1
        assert queries[0].query_type == "promql"
        assert queries[0].panel_title == "CPU Panel"
        assert queries[0].dashboard_uid == "dash1"

    def test_parse_logql(self):
        """LogQLクエリのパース."""
        dashboard = DashboardInfo(uid="dash1", title="Logs Dashboard")
        text = json.dumps(
            [
                {"title": "Error Logs", "expr": '{job="nginx"} |= "error"'},
            ]
        )
        queries = self.agent._parse_panel_queries(text, dashboard)

        assert len(queries) == 1
        assert queries[0].query_type == "logql"

    def test_parse_mixed(self):
        """混合クエリのパース."""
        dashboard = DashboardInfo(uid="dash1", title="Mixed")
        text = json.dumps(
            [
                {"title": "CPU", "expr": "rate(cpu[5m])"},
                {"title": "Logs", "query": '{app="test"}'},
                {"title": "Empty"},  # クエリなし
            ]
        )
        queries = self.agent._parse_panel_queries(text, dashboard)

        assert len(queries) == 2
        promql = [q for q in queries if q.query_type == "promql"]
        logql = [q for q in queries if q.query_type == "logql"]
        assert len(promql) == 1
        assert len(logql) == 1

    def test_parse_invalid_json(self):
        """不正JSONはパースエラーにならず空リスト."""
        dashboard = DashboardInfo(uid="dash1", title="Test")
        queries = self.agent._parse_panel_queries("invalid json", dashboard)
        assert queries == []


class TestOrchestratorDiscoverDashboardQueries:
    """ダッシュボードクエリ探索のテスト."""

    def setup_method(self):
        self.agent, _ = _make_orchestrator()

    @pytest.mark.asyncio
    async def test_discover_with_keywords(self):
        """キーワードでダッシュボード探索."""
        env = EnvironmentContext(
            investigation_keywords=["cpu"],
            available_dashboards=[
                DashboardInfo(uid="1", title="Memory Dashboard"),
                DashboardInfo(uid="2", title="CPU Monitoring"),
                DashboardInfo(uid="3", title="Network"),
            ],
        )

        mock_grafana = AsyncMock()
        mock_grafana.get_dashboard_panel_queries = AsyncMock(
            return_value={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            [
                                {"title": "CPU Usage", "expr": "rate(cpu[5m])"},
                            ]
                        ),
                    }
                ],
            }
        )

        await self.agent._discover_dashboard_queries(mock_grafana, env)

        # CPUダッシュボードが優先される
        assert "2" in env.explored_dashboard_uids
        assert len(env.discovered_panel_queries) > 0

    @pytest.mark.asyncio
    async def test_skip_already_explored(self):
        """探索済みダッシュボードはスキップ."""
        env = EnvironmentContext(
            available_dashboards=[
                DashboardInfo(uid="1", title="Test"),
            ],
            explored_dashboard_uids=["1"],
        )

        mock_grafana = AsyncMock()
        mock_grafana.get_dashboard_panel_queries = AsyncMock()

        await self.agent._discover_dashboard_queries(mock_grafana, env)

        # 既に探索済みなのでcall_toolは呼ばれない
        mock_grafana.get_dashboard_panel_queries.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_dashboards_limit(self):
        """最大ダッシュボード数の制限."""
        env = EnvironmentContext(
            available_dashboards=[DashboardInfo(uid=str(i), title=f"Dashboard {i}") for i in range(10)],
        )

        mock_grafana = AsyncMock()
        mock_grafana.get_dashboard_panel_queries = AsyncMock(
            return_value={
                "content": [{"type": "text", "text": "[]"}],
            }
        )

        await self.agent._discover_dashboard_queries(mock_grafana, env, max_dashboards=3)

        # 最大3つまで探索
        assert len(env.explored_dashboard_uids) == 3


# ================================================================
# Orchestrator refresh_health テスト
# ================================================================


class TestOrchestratorRefreshHealth:
    """OrchestratorAgent.refresh_health のテスト."""

    def _make_registry(
        self,
        *,
        prometheus_healthy: bool = True,
        loki_healthy: bool = True,
        grafana_healthy: bool = True,
    ) -> MagicMock:
        """指定した健全性でモックToolRegistryを生成."""
        mock_mcp = _make_mock_mcp()
        registry = MagicMock(spec=ToolRegistry)
        registry.prometheus = MCPConnection(name="prometheus", client=mock_mcp, healthy=prometheus_healthy)
        registry.loki = MCPConnection(name="loki", client=mock_mcp, healthy=loki_healthy)
        registry.grafana = MCPConnection(name="grafana", client=mock_mcp, healthy=grafana_healthy)
        return registry

    def test_all_healthy(self):
        """全MCPが健全な場合、全サブエージェントが生成される."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = self._make_registry()
        agent = OrchestratorAgent(llm=llm, registry=registry)

        new_registry = self._make_registry()
        result = agent.refresh_health(new_registry)

        assert result == {"prometheus": True, "loki": True, "grafana": True}
        assert agent.metrics_agent is not None
        assert agent.logs_agent is not None
        assert agent.grafana_mcp is not None

    def test_loki_down_logs_agent_none(self):
        """LokiダウンでGrafanaも無い場合、LogsAgentがNoneになる."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = self._make_registry()
        agent = OrchestratorAgent(llm=llm, registry=registry)

        # Lokiダウン + Grafanaもダウン（LogsAgentはどちらかが必要）
        down_registry = self._make_registry(loki_healthy=False, grafana_healthy=False)
        result = agent.refresh_health(down_registry)

        assert result["loki"] is False
        assert result["grafana"] is False
        assert agent.logs_agent is None

    def test_all_mcp_down_no_crash(self):
        """全MCPダウンでもクラッシュしない."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = self._make_registry()
        agent = OrchestratorAgent(llm=llm, registry=registry)

        down_registry = self._make_registry(
            prometheus_healthy=False,
            loki_healthy=False,
            grafana_healthy=False,
        )
        result = agent.refresh_health(down_registry)

        assert result == {"prometheus": False, "loki": False, "grafana": False}
        assert agent.metrics_agent is None
        assert agent.logs_agent is None
        assert agent.grafana_mcp is None
        # グラフはcompileできる（直接evaluate_resultsに遷移）
        compiled = agent.compile()
        assert compiled is not None

    def test_refresh_rebuilds_graph(self):
        """refresh_healthがグラフを実際に再構築する."""
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        registry = self._make_registry()
        agent = OrchestratorAgent(llm=llm, registry=registry)

        # 初期状態: MetricsAgent有り
        assert agent.metrics_agent is not None
        graph_before = agent.graph

        # Prometheusダウン + Grafanaもダウン → MetricsAgent消滅
        down_registry = self._make_registry(prometheus_healthy=False, grafana_healthy=False)
        agent.refresh_health(down_registry)

        assert agent.metrics_agent is None
        # グラフが再構築された（異なるオブジェクト）
        assert agent.graph is not graph_before
