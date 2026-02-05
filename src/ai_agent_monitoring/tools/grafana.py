"""Grafana MCP Tool — ダッシュボード・アラート操作."""

import logging
from datetime import datetime
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool

from ai_agent_monitoring.tools.base import BaseMCPTool, MCPClient

logger = logging.getLogger(__name__)


class GrafanaMCPTool(BaseMCPTool):
    """Grafana MCP Server 経由のダッシュボード・アラート操作ツール群.

    grafana/mcp-grafana が提供する機能をラップする。
    PromQL/LogQLの実行もGrafana MCP経由で可能。

    セッション再利用:
        複数のツールを連続で呼び出す場合は session_context() を使用して
        セッションを再利用することを推奨。これによりSSE接続の
        オーバーヘッドを削減できる。

        使用例:
            async with grafana_tool.session_context() as ctx:
                dashboards = await ctx.list_dashboards()
                alerts = await ctx.get_firing_alerts()
    """

    async def list_dashboards(self) -> dict[str, Any]:
        """ダッシュボード一覧を取得."""
        logger.info("Grafana: list dashboards")
        return await self._call_tool("list_dashboards", {})

    async def get_dashboard_by_uid(self, uid: str) -> dict[str, Any]:
        """UIDを指定してダッシュボードの詳細を取得."""
        logger.info("Grafana: get dashboard uid=%s", uid)
        return await self._call_tool("get_dashboard_by_uid", {"uid": uid})

    async def get_dashboard_panels(self, uid: str) -> dict[str, Any]:
        """ダッシュボードのパネル一覧を取得."""
        logger.info("Grafana: get panels for dashboard uid=%s", uid)
        return await self._call_tool("get_dashboard_panels", {"uid": uid})

    async def query_prometheus(
        self,
        datasource_uid: str,
        expr: str,
        start: datetime | None = None,
        end: datetime | None = None,
        step_seconds: int = 60,
        query_type: str = "range",
    ) -> dict[str, Any]:
        """Grafana経由でPromQLクエリを実行.

        Args:
            datasource_uid: データソースのUID（必須）
            expr: PromQLクエリ式（必須）
            start: 開始時刻（必須）
            end: 終了時刻（rangeクエリの場合必須）
            step_seconds: 時系列のステップサイズ（秒）
            query_type: クエリタイプ（"range" or "instant"）
        """
        # startTimeは必須
        if not start:
            start = datetime.now()

        params: dict[str, Any] = {
            "datasourceUid": datasource_uid,
            "expr": expr,
            "startTime": start.isoformat(),
            "queryType": query_type,
        }
        if end:
            params["endTime"] = end.isoformat()
        if query_type == "range":
            params["stepSeconds"] = step_seconds

        logger.info("Grafana: PromQL query: %s (datasource=%s)", expr, datasource_uid)
        return await self._call_tool("query_prometheus", params)

    async def query_loki(
        self,
        datasource_uid: str,
        logql: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Grafana経由でLogQLクエリを実行.

        Args:
            datasource_uid: データソースのUID（必須）
            logql: LogQLクエリ（必須）
            start: 開始時刻
            end: 終了時刻
            limit: 返すログ行の最大数（最大100）
            direction: クエリの方向（"forward" or "backward"）
        """
        params: dict[str, Any] = {
            "datasourceUid": datasource_uid,
            "logql": logql,
            "limit": min(limit, 100),  # 最大100
            "direction": direction,
        }
        if start:
            params["startRfc3339"] = start.isoformat()
        if end:
            params["endRfc3339"] = end.isoformat()

        logger.info("Grafana: LogQL query: %s (datasource=%s)", logql, datasource_uid)
        return await self._call_tool("query_loki_logs", params)

    async def list_alert_rules(self) -> dict[str, Any]:
        """アラートルール一覧を取得."""
        logger.info("Grafana: list alert rules")
        return await self._call_tool("list_alert_rules", {})

    async def get_alert_rule(self, uid: str) -> dict[str, Any]:
        """特定のアラートルールを取得."""
        logger.info("Grafana: get alert rule uid=%s", uid)
        return await self._call_tool("get_alert_rule", {"uid": uid})

    async def get_firing_alerts(self) -> dict[str, Any]:
        """現在発火中のアラートを取得."""
        logger.info("Grafana: get firing alerts")
        return await self._call_tool("get_firing_alerts", {})

    async def render_panel_image(
        self,
        dashboard_uid: str,
        panel_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        width: int = 800,
        height: int = 400,
    ) -> bytes:
        """パネルをPNG画像としてレンダリング.

        Grafana Render API (/render/d-solo/) を使用。
        """
        params: dict[str, Any] = {
            "panelId": panel_id,
            "width": width,
            "height": height,
        }
        if start:
            params["from"] = str(int(start.timestamp() * 1000))
        if end:
            params["to"] = str(int(end.timestamp() * 1000))

        logger.info(
            "Grafana: render panel image dashboard=%s panel=%d",
            dashboard_uid,
            panel_id,
        )
        async with httpx.AsyncClient(timeout=self.mcp_client.timeout) as client:
            response = await client.get(
                f"{self.mcp_client.base_url}/render/d-solo/{dashboard_uid}",
                params=params,
            )
            response.raise_for_status()
            return response.content

    async def search_dashboards(self, query: str) -> dict[str, Any]:
        """ダッシュボードをキーワード検索."""
        logger.info("Grafana: search dashboards query=%s", query)
        return await self._call_tool("search_dashboards", {"query": query})

    # ===========================================================
    # 環境発見ツール（Discovery Tools）
    # ===========================================================

    async def list_datasources(self, ds_type: str = "") -> dict[str, Any]:
        """データソース一覧を取得.

        Args:
            ds_type: フィルタするデータソースタイプ（例: prometheus, loki）
        """
        logger.info("Grafana: list datasources type=%s", ds_type or "all")
        params: dict[str, Any] = {}
        if ds_type:
            params["type"] = ds_type
        return await self._call_tool("list_datasources", params)

    async def list_prometheus_metric_names(
        self,
        datasource_uid: str,
        regex: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Prometheusで利用可能なメトリクス名一覧を取得.

        Args:
            datasource_uid: データソースのUID
            regex: フィルタ用の正規表現
            limit: 取得件数上限
        """
        logger.info("Grafana: list prometheus metrics datasource=%s", datasource_uid)
        params: dict[str, Any] = {"datasourceUid": datasource_uid, "limit": limit}
        if regex:
            params["regex"] = regex
        return await self._call_tool("list_prometheus_metric_names", params)

    async def list_prometheus_label_names(
        self,
        datasource_uid: str,
        matches: str = "",
    ) -> dict[str, Any]:
        """Prometheusで利用可能なラベル名一覧を取得.

        Args:
            datasource_uid: データソースのUID
            matches: フィルタ用のメトリクスセレクタ
        """
        logger.info("Grafana: list prometheus label names datasource=%s", datasource_uid)
        params: dict[str, Any] = {"datasourceUid": datasource_uid}
        if matches:
            params["matches"] = matches
        return await self._call_tool("list_prometheus_label_names", params)

    async def list_prometheus_label_values(
        self,
        datasource_uid: str,
        label_name: str,
        matches: str = "",
    ) -> dict[str, Any]:
        """Prometheusの特定ラベルの値一覧を取得.

        Args:
            datasource_uid: データソースのUID
            label_name: ラベル名
            matches: フィルタ用のメトリクスセレクタ
        """
        logger.info(
            "Grafana: list prometheus label values datasource=%s label=%s",
            datasource_uid,
            label_name,
        )
        params: dict[str, Any] = {
            "datasourceUid": datasource_uid,
            "labelName": label_name,
        }
        if matches:
            params["matches"] = matches
        return await self._call_tool("list_prometheus_label_values", params)

    async def list_loki_label_names(
        self,
        datasource_uid: str,
    ) -> dict[str, Any]:
        """Lokiで利用可能なラベル名一覧を取得.

        Args:
            datasource_uid: データソースのUID
        """
        logger.info("Grafana: list loki label names datasource=%s", datasource_uid)
        return await self._call_tool("list_loki_label_names", {"datasourceUid": datasource_uid})

    async def list_loki_label_values(
        self,
        datasource_uid: str,
        label_name: str,
    ) -> dict[str, Any]:
        """Lokiの特定ラベルの値一覧を取得.

        Args:
            datasource_uid: データソースのUID
            label_name: ラベル名
        """
        logger.info(
            "Grafana: list loki label values datasource=%s label=%s",
            datasource_uid,
            label_name,
        )
        return await self._call_tool(
            "list_loki_label_values",
            {"datasourceUid": datasource_uid, "labelName": label_name},
        )

    async def get_dashboard_panel_queries(self, uid: str) -> dict[str, Any]:
        """ダッシュボードのパネルで使用されているクエリを取得.

        Args:
            uid: ダッシュボードのUID
        """
        logger.info("Grafana: get dashboard panel queries uid=%s", uid)
        return await self._call_tool("get_dashboard_panel_queries", {"uid": uid})


def create_grafana_tools(mcp_client: MCPClient) -> list[BaseTool]:
    """LangChain Tool としてラップされた Grafana ツール群を生成."""
    grafana = GrafanaMCPTool(mcp_client)

    @tool
    async def grafana_list_dashboards() -> dict[str, Any]:
        """Grafanaのダッシュボード一覧を取得します。"""
        return await grafana.list_dashboards()

    @tool
    async def grafana_get_dashboard(uid: str) -> dict[str, Any]:
        """指定UIDのGrafanaダッシュボードの詳細を取得します。"""
        return await grafana.get_dashboard_by_uid(uid)

    @tool
    async def grafana_search_dashboards(query: str) -> dict[str, Any]:
        """キーワードでGrafanaダッシュボードを検索します。"""
        return await grafana.search_dashboards(query)

    @tool
    async def grafana_query_prometheus(
        datasource_uid: str,
        expr: str,
        start: str = "",
        end: str = "",
        step_seconds: int = 60,
        query_type: str = "range",
    ) -> dict[str, Any]:
        """Grafana経由でPromQLクエリを実行します。

        Args:
            datasource_uid: Prometheusデータソースのuid（必須）
            expr: PromQLクエリ式（必須）
            start: 開始時刻（ISO 8601形式、必須）
            end: 終了時刻（ISO 8601形式、rangeクエリの場合必須）
            step_seconds: 時系列のステップサイズ（秒）
            query_type: 'range' または 'instant'
        """
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await grafana.query_prometheus(datasource_uid, expr, s, e, step_seconds, query_type)

    @tool
    async def grafana_query_loki(
        datasource_uid: str,
        logql: str,
        start: str = "",
        end: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Grafana経由でLogQLクエリを実行します。

        Args:
            datasource_uid: Lokiデータソースのuid（必須）
            logql: LogQLクエリ（必須）。{job="xxx"} |= "error" の形式
            start: 開始時刻（ISO 8601形式）
            end: 終了時刻（ISO 8601形式）
            limit: 返すログ行の最大数（最大100）
        """
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await grafana.query_loki(datasource_uid, logql, s, e, limit)

    @tool
    async def grafana_list_alert_rules() -> dict[str, Any]:
        """Grafanaのアラートルール一覧を取得します。"""
        return await grafana.list_alert_rules()

    @tool
    async def grafana_get_firing_alerts() -> dict[str, Any]:
        """現在発火中のGrafanaアラートを取得します。"""
        return await grafana.get_firing_alerts()

    # ===========================================================
    # 環境発見ツール
    # ===========================================================

    @tool
    async def grafana_list_datasources(ds_type: str = "") -> dict[str, Any]:
        """Grafanaに登録されているデータソース一覧を取得します。
        ds_typeでprometheus/lokiなどでフィルタできます。"""
        return await grafana.list_datasources(ds_type)

    @tool
    async def grafana_list_prometheus_metrics(
        datasource_uid: str,
        regex: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Prometheusで利用可能なメトリクス名一覧を取得します。
        datasource_uidはgrafana_list_datasourcesで取得できます。"""
        return await grafana.list_prometheus_metric_names(datasource_uid, regex, limit)

    @tool
    async def grafana_list_prometheus_labels(
        datasource_uid: str,
        matches: str = "",
    ) -> dict[str, Any]:
        """Prometheusで利用可能なラベル名一覧を取得します。"""
        return await grafana.list_prometheus_label_names(datasource_uid, matches)

    @tool
    async def grafana_list_prometheus_label_values(
        datasource_uid: str,
        label_name: str,
        matches: str = "",
    ) -> dict[str, Any]:
        """Prometheusの特定ラベルの値一覧を取得します。
        例: label_name='job'でjobラベルの全値を取得。"""
        return await grafana.list_prometheus_label_values(datasource_uid, label_name, matches)

    @tool
    async def grafana_list_loki_labels(datasource_uid: str) -> dict[str, Any]:
        """Lokiで利用可能なラベル名一覧を取得します。"""
        return await grafana.list_loki_label_names(datasource_uid)

    @tool
    async def grafana_list_loki_label_values(
        datasource_uid: str,
        label_name: str,
    ) -> dict[str, Any]:
        """Lokiの特定ラベルの値一覧を取得します。"""
        return await grafana.list_loki_label_values(datasource_uid, label_name)

    @tool
    async def grafana_get_panel_queries(uid: str) -> dict[str, Any]:
        """ダッシュボードのパネルで使用されているPromQL/LogQLクエリを取得します。
        既存のダッシュボードからクエリパターンを学習するのに便利です。"""
        return await grafana.get_dashboard_panel_queries(uid)

    return [
        # 既存ツール
        grafana_list_dashboards,
        grafana_get_dashboard,
        grafana_search_dashboards,
        grafana_query_prometheus,
        grafana_query_loki,
        grafana_list_alert_rules,
        grafana_get_firing_alerts,
        # 環境発見ツール
        grafana_list_datasources,
        grafana_list_prometheus_metrics,
        grafana_list_prometheus_labels,
        grafana_list_prometheus_label_values,
        grafana_list_loki_labels,
        grafana_list_loki_label_values,
        grafana_get_panel_queries,
    ]
