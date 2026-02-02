"""tools のテスト."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import GrafanaMCPTool, create_grafana_tools
from ai_agent_monitoring.tools.loki import LokiMCPTool, create_loki_tools
from ai_agent_monitoring.tools.prometheus import PrometheusMCPTool, create_prometheus_tools
from ai_agent_monitoring.tools.registry import ToolRegistry


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_call_tool(self):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"result": "ok"}
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            client = MCPClient("http://localhost:8080")
            result = await client.call_tool("test_tool", {"key": "value"})

            assert result == {"result": "ok"}
            mock_client.post.assert_called_once_with(
                "http://localhost:8080/tools/call",
                json={"tool": "test_tool", "parameters": {"key": "value"}},
            )


class TestPrometheusMCPTool:
    @pytest.mark.asyncio
    async def test_instant_query(self, mock_mcp_client):
        prom = PrometheusMCPTool(mock_mcp_client)
        await prom.instant_query("up")

        mock_mcp_client.call_tool.assert_called_once_with(
            "query_prometheus",
            {"query": "up"},
        )

    @pytest.mark.asyncio
    async def test_range_query(self, mock_mcp_client):
        prom = PrometheusMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await prom.range_query("rate(cpu[5m])", start, end, "1m")

        mock_mcp_client.call_tool.assert_called_once()
        call_args = mock_mcp_client.call_tool.call_args
        assert call_args[0][0] == "query_prometheus"
        assert call_args[0][1]["type"] == "range"

    def test_create_tools(self, mock_mcp_client):
        tools = create_prometheus_tools(mock_mcp_client)
        assert len(tools) == 2
        names = [t.name for t in tools]
        assert "query_prometheus_instant" in names
        assert "query_prometheus_range" in names


class TestLokiMCPTool:
    @pytest.mark.asyncio
    async def test_query_logs(self, mock_mcp_client):
        loki = LokiMCPTool(mock_mcp_client)
        await loki.query_logs('{job="myapp"}', limit=50)

        mock_mcp_client.call_tool.assert_called_once_with(
            "query_loki",
            {"query": '{job="myapp"}', "limit": 50},
        )

    @pytest.mark.asyncio
    async def test_query_logs_with_time_range(self, mock_mcp_client):
        loki = LokiMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await loki.query_logs('{job="myapp"}', start=start, end=end)

        call_args = mock_mcp_client.call_tool.call_args[0][1]
        assert "start" in call_args
        assert "end" in call_args
        assert call_args["start"] == start.isoformat()
        assert call_args["end"] == end.isoformat()

    @pytest.mark.asyncio
    async def test_query_metrics(self, mock_mcp_client):
        loki = LokiMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await loki.query_metrics('rate({job="myapp"}[5m])', start=start, end=end, step="1m")

        mock_mcp_client.call_tool.assert_called_once()
        call_args = mock_mcp_client.call_tool.call_args
        assert call_args[0][0] == "query_loki_metrics"
        assert call_args[0][1]["step"] == "1m"
        assert "start" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_find_error_patterns_with_time(self, mock_mcp_client):
        loki = LokiMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await loki.find_error_patterns("myapp", start=start, end=end)

        call_args = mock_mcp_client.call_tool.call_args[0][1]
        assert call_args["service"] == "myapp"
        assert "start" in call_args
        assert "end" in call_args

    @pytest.mark.asyncio
    async def test_find_error_patterns(self, mock_mcp_client):
        loki = LokiMCPTool(mock_mcp_client)
        await loki.find_error_patterns("myapp")

        mock_mcp_client.call_tool.assert_called_once_with(
            "find_error_patterns",
            {"service": "myapp"},
        )

    def test_create_tools(self, mock_mcp_client):
        tools = create_loki_tools(mock_mcp_client)
        assert len(tools) == 2


class TestLokiToolFunctions:
    """create_loki_tools で生成されるLangChainツール関数のテスト."""

    @pytest.mark.asyncio
    async def test_query_loki_logs_tool(self, mock_mcp_client):
        tools = create_loki_tools(mock_mcp_client)
        query_tool = next(t for t in tools if t.name == "query_loki_logs")

        await query_tool.ainvoke({"query": '{job="app"}', "start": "", "end": "", "limit": 50})
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_query_loki_logs_with_iso_time(self, mock_mcp_client):
        tools = create_loki_tools(mock_mcp_client)
        query_tool = next(t for t in tools if t.name == "query_loki_logs")

        await query_tool.ainvoke({
            "query": '{job="app"}',
            "start": "2026-02-01T15:00:00+00:00",
            "end": "2026-02-01T16:00:00+00:00",
            "limit": 100,
        })
        call_args = mock_mcp_client.call_tool.call_args[0][1]
        assert "start" in call_args

    @pytest.mark.asyncio
    async def test_find_service_errors_tool(self, mock_mcp_client):
        tools = create_loki_tools(mock_mcp_client)
        error_tool = next(t for t in tools if t.name == "find_service_errors")

        await error_tool.ainvoke({"service": "myapp", "start": "", "end": ""})
        mock_mcp_client.call_tool.assert_called()


class TestGrafanaMCPTool:
    @pytest.mark.asyncio
    async def test_list_dashboards(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.list_dashboards()

        mock_mcp_client.call_tool.assert_called_once_with("list_dashboards", {})

    @pytest.mark.asyncio
    async def test_get_dashboard_by_uid(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.get_dashboard_by_uid("test-uid")

        mock_mcp_client.call_tool.assert_called_once_with(
            "get_dashboard_by_uid", {"uid": "test-uid"},
        )

    @pytest.mark.asyncio
    async def test_get_dashboard_panels(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.get_dashboard_panels("test-uid")

        mock_mcp_client.call_tool.assert_called_once_with(
            "get_dashboard_panels", {"uid": "test-uid"},
        )

    @pytest.mark.asyncio
    async def test_query_prometheus(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await grafana.query_prometheus("up", start=start, end=end, step="5m")

        call_args = mock_mcp_client.call_tool.call_args
        assert call_args[0][0] == "query_prometheus"
        params = call_args[0][1]
        assert params["query"] == "up"
        assert params["step"] == "5m"
        assert "start" in params
        assert "end" in params

    @pytest.mark.asyncio
    async def test_query_loki(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        await grafana.query_loki('{job="app"}', start=start, end=end, limit=50)

        call_args = mock_mcp_client.call_tool.call_args
        params = call_args[0][1]
        assert params["limit"] == 50
        assert "start" in params

    @pytest.mark.asyncio
    async def test_list_alert_rules(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.list_alert_rules()
        mock_mcp_client.call_tool.assert_called_once_with("list_alert_rules", {})

    @pytest.mark.asyncio
    async def test_get_alert_rule(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.get_alert_rule("rule-uid")
        mock_mcp_client.call_tool.assert_called_once_with(
            "get_alert_rule", {"uid": "rule-uid"},
        )

    @pytest.mark.asyncio
    async def test_get_firing_alerts(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.get_firing_alerts()

        mock_mcp_client.call_tool.assert_called_once_with("get_firing_alerts", {})

    @pytest.mark.asyncio
    async def test_search_dashboards(self, mock_mcp_client):
        grafana = GrafanaMCPTool(mock_mcp_client)
        await grafana.search_dashboards("cpu")
        mock_mcp_client.call_tool.assert_called_once_with(
            "search_dashboards", {"query": "cpu"},
        )

    @pytest.mark.asyncio
    async def test_render_panel_image(self, mock_mcp_client):
        """render_panel_image は httpx を直接使用する."""
        grafana = GrafanaMCPTool(mock_mcp_client)

        mock_response = MagicMock()
        mock_response.content = b"\x89PNG fake image"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        start = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await grafana.render_panel_image(
                "dash-uid", 1, start=start, end=end, width=800, height=400,
            )

        assert result == b"\x89PNG fake image"
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "dash-uid" in call_args[0][0]
        assert call_args[1]["params"]["panelId"] == 1

    @pytest.mark.asyncio
    async def test_render_panel_image_no_time(self, mock_mcp_client):
        """時間範囲なしでrender_panel_imageを呼び出す."""
        grafana = GrafanaMCPTool(mock_mcp_client)

        mock_response = MagicMock()
        mock_response.content = b"\x89PNG"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await grafana.render_panel_image("uid", 2)

        params = mock_client.get.call_args[1]["params"]
        assert "from" not in params
        assert "to" not in params

    def test_create_tools(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        assert len(tools) == 7
        names = [t.name for t in tools]
        assert "grafana_list_dashboards" in names
        assert "grafana_query_prometheus" in names
        assert "grafana_get_firing_alerts" in names


class TestGrafanaToolFunctions:
    """create_grafana_tools で生成されるLangChainツール関数のテスト."""

    @pytest.mark.asyncio
    async def test_grafana_list_dashboards(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_list_dashboards")
        await tool.ainvoke({})
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_grafana_get_dashboard(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_get_dashboard")
        await tool.ainvoke({"uid": "test"})
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_grafana_search_dashboards(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_search_dashboards")
        await tool.ainvoke({"query": "cpu"})
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_grafana_query_prometheus_with_time(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_query_prometheus")
        await tool.ainvoke({
            "query": "up",
            "start": "2026-02-01T15:00:00+00:00",
            "end": "2026-02-01T16:00:00+00:00",
            "step": "1m",
        })
        call_args = mock_mcp_client.call_tool.call_args[0][1]
        assert "start" in call_args

    @pytest.mark.asyncio
    async def test_grafana_query_loki_with_time(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_query_loki")
        await tool.ainvoke({
            "query": '{job="app"}',
            "start": "2026-02-01T15:00:00+00:00",
            "end": "2026-02-01T16:00:00+00:00",
            "limit": 100,
        })
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_grafana_list_alert_rules(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_list_alert_rules")
        await tool.ainvoke({})
        mock_mcp_client.call_tool.assert_called()

    @pytest.mark.asyncio
    async def test_grafana_get_firing_alerts(self, mock_mcp_client):
        tools = create_grafana_tools(mock_mcp_client)
        tool = next(t for t in tools if t.name == "grafana_get_firing_alerts")
        await tool.ainvoke({})
        mock_mcp_client.call_tool.assert_called()


class TestToolRegistry:
    def test_from_settings(self, settings):
        registry = ToolRegistry.from_settings(settings)

        assert registry.prometheus.name == "prometheus"
        assert registry.loki.name == "loki"
        assert registry.grafana.name == "grafana"
        assert registry.prometheus.client.base_url == "http://localhost:9090"

    @pytest.mark.asyncio
    async def test_health_check_all_down(self, settings):
        registry = ToolRegistry.from_settings(settings)
        # 実際の接続はないのですべてunhealthy
        results = await registry.health_check()

        assert results["prometheus"] is False
        assert results["loki"] is False
        assert results["grafana"] is False

    def test_create_all_tools(self, settings):
        registry = ToolRegistry.from_settings(settings)
        tools = registry.create_all_tools()

        # prometheus(2) + loki(2) + grafana(7) = 11
        assert len(tools) == 11

    def test_get_healthy_connections(self, settings):
        registry = ToolRegistry.from_settings(settings)
        registry.prometheus.healthy = True
        registry.loki.healthy = False
        registry.grafana.healthy = True

        healthy = registry.get_healthy_connections()
        assert len(healthy) == 2
        names = [c.name for c in healthy]
        assert "prometheus" in names
        assert "grafana" in names
