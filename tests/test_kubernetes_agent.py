"""KubernetesAgent のテスト."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ai_agent_monitoring.agents.kubernetes_agent import KubernetesAgent
from ai_agent_monitoring.core.models import KubernetesResult
from ai_agent_monitoring.core.state import InvestigationPlan, TimeRange
from ai_agent_monitoring.tools.base import MCPClient

# ---- ヘルパー ----


def _make_mock_mcp() -> MCPClient:
    """モックMCPクライアントを生成."""
    mock_mcp = MagicMock(spec=MCPClient)
    mock_mcp.base_url = "http://mock-k8s:8080"
    mock_mcp.timeout = 30.0
    mock_mcp.call_tool = AsyncMock(return_value={"status": "ok", "data": []})
    return mock_mcp


def _make_kubernetes_agent(with_mcp: bool = True) -> tuple[KubernetesAgent, MagicMock]:
    """テスト用KubernetesAgentを生成."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    mock_mcp = _make_mock_mcp() if with_mcp else None
    agent = KubernetesAgent(llm, kubernetes_mcp=mock_mcp)
    return agent, llm


# ================================================================
# 初期化テスト
# ================================================================


class TestKubernetesAgentInit:
    """KubernetesAgent の初期化テスト."""

    def test_init_with_mcp(self):
        """MCPクライアントありの場合、ツールが生成される."""
        agent, llm = _make_kubernetes_agent(with_mcp=True)
        assert len(agent.tools) == 8
        llm.bind_tools.assert_called_once()

    def test_init_without_mcp(self):
        """MCPクライアントなしの場合、ツールは空."""
        agent, _llm = _make_kubernetes_agent(with_mcp=False)
        assert len(agent.tools) == 0

    def test_compile(self):
        """コンパイルが正常に完了する."""
        agent, _ = _make_kubernetes_agent()
        compiled = agent.compile()
        assert compiled is not None


# ================================================================
# ReActループテスト
# ================================================================


class TestKubernetesAgentReason:
    """KubernetesAgent の _reason テスト."""

    def setup_method(self):
        self.agent, self.llm = _make_kubernetes_agent()

    @pytest.mark.asyncio
    async def test_reason_no_plan(self):
        """計画なしの場合、エラーメッセージを返す."""
        state: dict[str, Any] = {"messages": [], "plan": None}
        result = await self.agent._reason(state)
        messages = result["messages"]
        assert len(messages) == 1
        assert isinstance(messages[0], AIMessage)
        assert "調査計画がありません" in messages[0].content

    @pytest.mark.asyncio
    async def test_reason_with_plan(self):
        """計画ありの場合、システムプロンプトと調査指示を含む."""
        plan = InvestigationPlan(
            target_namespaces=["monitoring"],
            target_pods=["prometheus-0"],
            k8s_resource_kinds=["Deployment", "Service"],
            target_instances=["node-01"],
            time_range=TimeRange(
                start="2026-02-01T15:00:00Z",
                end="2026-02-01T16:00:00Z",
            ),
        )
        response = MagicMock()
        response.content = "K8sクラスタを調査します。"
        response.tool_calls = []
        self.llm.ainvoke = AsyncMock(return_value=response)

        state: dict[str, Any] = {"messages": [], "plan": plan}
        result = await self.agent._reason(state)
        messages = result["messages"]

        # SystemMessage + HumanMessage + AIMessage(response)
        assert len(messages) == 3
        assert isinstance(messages[0], SystemMessage)
        assert "Kubernetes Agent" in messages[0].content
        assert isinstance(messages[1], HumanMessage)
        assert "monitoring" in messages[1].content
        assert "prometheus-0" in messages[1].content

    @pytest.mark.asyncio
    async def test_reason_with_plan_no_k8s_fields(self):
        """K8sフィールドなしの計画でもエラーにならない."""
        plan = InvestigationPlan(
            promql_queries=["up"],
        )
        response = MagicMock()
        response.content = "調査を開始します。"
        response.tool_calls = []
        self.llm.ainvoke = AsyncMock(return_value=response)

        state: dict[str, Any] = {"messages": [], "plan": plan}
        result = await self.agent._reason(state)
        messages = result["messages"]
        assert len(messages) == 3
        assert "全般的な調査" in messages[1].content


# ================================================================
# サマライズテスト
# ================================================================


class TestKubernetesAgentSummarize:
    """KubernetesAgent の _summarize テスト."""

    def setup_method(self):
        self.agent, self.llm = _make_kubernetes_agent()

    @pytest.mark.asyncio
    async def test_summarize_returns_k8s_result(self):
        """_summarize が KubernetesResult を返す."""
        response = MagicMock()
        response.content = "Pod monitoring/prometheus-0 が CrashLoopBackOff 状態。OOMKilled検出。"
        response.tool_calls = []
        self.llm.ainvoke = AsyncMock(return_value=response)

        state: dict[str, Any] = {
            "messages": [HumanMessage(content="調査結果をまとめてください")],
            "plan": InvestigationPlan(),
        }
        result = await self.agent._summarize(state)

        assert "k8s_results" in result
        assert len(result["k8s_results"]) == 1
        k8s_result = result["k8s_results"][0]
        assert isinstance(k8s_result, KubernetesResult)
        assert "CrashLoopBackOff" in k8s_result.summary


# ================================================================
# ルーティングテスト
# ================================================================


class TestKubernetesAgentRouting:
    """KubernetesAgent の _should_use_tool テスト."""

    def test_should_use_tool_with_tool_calls(self):
        """tool_callsがある場合、'tool_call'を返す."""
        msg = MagicMock()
        msg.tool_calls = [{"name": "k8s_list_pods", "args": {}}]
        state: dict[str, Any] = {"messages": [msg]}
        assert KubernetesAgent._should_use_tool(state) == "tool_call"

    def test_should_use_tool_without_tool_calls(self):
        """tool_callsがない場合、'done'を返す."""
        msg = MagicMock()
        msg.tool_calls = []
        state: dict[str, Any] = {"messages": [msg]}
        assert KubernetesAgent._should_use_tool(state) == "done"

    def test_should_use_tool_no_attr(self):
        """tool_calls属性がない場合、'done'を返す."""
        msg = MagicMock(spec=[])  # no attributes
        state: dict[str, Any] = {"messages": [msg]}
        assert KubernetesAgent._should_use_tool(state) == "done"
