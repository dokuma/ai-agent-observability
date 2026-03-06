"""API エンドポイントのテスト."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ai_agent_monitoring.api.dependencies import app_state
from ai_agent_monitoring.api.main import app
from ai_agent_monitoring.api.schemas import ReportSearchResponse
from ai_agent_monitoring.core.models import RCAReport, RootCause, TriggerType


@pytest.fixture
def client():
    """テスト用 FastAPI クライアント."""
    return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    def test_health_unhealthy_no_registry(self, client):
        app_state.registry = None
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unhealthy"

    def test_health_all_healthy(self, client):
        """全MCPが正常な場合はhealthy."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(
            return_value={
                "prometheus": True,
                "loki": True,
                "grafana": True,
            }
        )
        app_state.registry = mock_registry

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["mcp_servers"]["prometheus"] is True

    def test_health_degraded(self, client):
        """一部のMCPがunhealthyな場合はdegraded."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(
            return_value={
                "prometheus": True,
                "loki": False,
                "grafana": True,
            }
        )
        app_state.registry = mock_registry

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"

    def test_health_all_unhealthy(self, client):
        """全MCPがunhealthyな場合はunhealthy."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(
            return_value={
                "prometheus": False,
                "loki": False,
                "grafana": False,
            }
        )
        app_state.registry = mock_registry

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unhealthy"


class TestAlertWebhook:
    def test_webhook_empty_alerts(self, client):
        response = client.post(
            "/api/v1/webhook/alertmanager",
            json={"alerts": []},
        )
        assert response.status_code == 400

    def test_webhook_valid_alert(self, client):
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled

        response = client.post(
            "/api/v1/webhook/alertmanager",
            json={
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "HighCPU",
                            "severity": "warning",
                            "instance": "web-01",
                        },
                        "annotations": {
                            "summary": "CPU high",
                        },
                        "startsAt": "2026-02-01T16:00:00Z",
                    }
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "investigation_id" in data


class TestUserQuery:
    def test_query_valid(self, client):
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled

        response = client.post(
            "/api/v1/query",
            json={"query": "昨日の4時ごろ異常がなかったか確認してください"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    def test_query_routed_to_report_search(self, client):
        """レポートがある場合、search インテントで report_search_agent にルーティング."""
        mock_store = MagicMock()
        mock_store.count.return_value = 3

        mock_search_agent = AsyncMock()
        mock_search_agent.search_and_answer.return_value = ReportSearchResponse(
            answer="問題のコンポーネントは monitoring 名前空間にあります。",
            results=[],
            total_reports=3,
        )

        app_state.report_store = mock_store
        app_state.report_search_agent = mock_search_agent

        # orchestrator の LLM を mock して "search" と返す
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content="search")
        mock_orchestrator = MagicMock()
        mock_orchestrator.llm = mock_llm
        app_state.orchestrator = mock_orchestrator

        response = client.post(
            "/api/v1/query",
            json={"query": "前回の問題のコンポーネントはどの名前空間にありますか？"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["routed_to"] == "report_search"
        assert data["status"] == "completed"
        assert "monitoring" in data["report_search_answer"]

        # cleanup
        app_state.report_store = None
        app_state.report_search_agent = None

    def test_query_routed_to_investigation_no_reports(self, client):
        """レポートがない場合、常に新規調査を開始."""
        app_state.report_store = MagicMock()
        app_state.report_store.count.return_value = 0

        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled

        response = client.post(
            "/api/v1/query",
            json={"query": "クラスタの状態を確認してください"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["routed_to"] == "investigation"
        assert data["status"] == "running"

        app_state.report_store = None

    def test_query_empty(self, client):
        response = client.post(
            "/api/v1/query",
            json={"query": ""},
        )
        assert response.status_code == 422  # validation error


class TestInvestigationStatus:
    def test_not_found(self, client):
        response = client.get("/api/v1/investigations/nonexistent")
        assert response.status_code == 404

    def test_get_status(self, client):
        inv_id = app_state.create_investigation("alert")
        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["trigger_type"] == "alert"


class TestInvestigationReport:
    def test_not_found(self, client):
        response = client.get("/api/v1/investigations/nonexistent/report")
        assert response.status_code == 404

    def test_still_running(self, client):
        inv_id = app_state.create_investigation("alert")
        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 409

    def test_failed(self, client):
        inv_id = app_state.create_investigation("alert")
        app_state.fail_investigation(inv_id, "test error")
        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 500

    def test_completed_with_report(self, client):
        inv_id = app_state.create_investigation("alert")
        report = RCAReport(
            trigger_type=TriggerType.ALERT,
            root_causes=[RootCause(description="test cause", confidence=0.8)],
            metrics_summary="test metrics",
            logs_summary="test logs",
            recommendations=["fix it"],
            markdown="# Test Report",
        )
        app_state.complete_investigation(inv_id, rca_report=report)

        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 200
        data = response.json()
        assert data["markdown"] == "# Test Report"
        assert len(data["root_causes"]) == 1
        assert data["root_causes"][0]["confidence"] == 0.8


class TestInvestigationStageUpdate:
    """調査ステージ更新のテスト."""

    def test_update_stage(self, client):
        """ステージが正しく更新される."""
        inv_id = app_state.create_investigation("user_query")

        # 初期状態
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == ""

        # ステージ更新
        app_state.update_investigation_stage(inv_id, "環境情報を収集中")
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == "環境情報を収集中"

        # ステージ更新（iteration_countも更新）
        app_state.update_investigation_stage(inv_id, "調査計画を策定中", iteration_count=2)
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == "調査計画を策定中"
        assert record.iteration_count == 2

    def test_update_stage_nonexistent(self, client):
        """存在しない調査IDでは何もしない."""
        # 例外が発生しないことを確認
        app_state.update_investigation_stage("nonexistent-id", "テスト")

    def test_status_includes_current_stage(self, client):
        """APIレスポンスにcurrent_stageが含まれる."""
        inv_id = app_state.create_investigation("user_query")
        app_state.update_investigation_stage(inv_id, "メトリクスを調査中", iteration_count=1)

        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["current_stage"] == "メトリクスを調査中"
        assert data["iteration_count"] == 1


class TestInvestigationTimeout:
    """調査タイムアウトのテスト."""

    @pytest.mark.asyncio
    async def test_investigation_timeout(self):
        """調査がタイムアウトした場合、failedステータスになる."""
        import asyncio

        from ai_agent_monitoring.api.routes import _run_user_query_investigation
        from ai_agent_monitoring.core.models import UserQuery

        # タイムアウトを短く設定
        app_state.settings.investigation_timeout_seconds = 1

        # 遅延するモックオーケストレータ
        mock_orchestrator = MagicMock()
        compiled = MagicMock()

        async def slow_invoke(*args, **kwargs):
            await asyncio.sleep(5)  # 5秒待機（タイムアウトより長い）
            return {"rca_report": None}

        compiled.ainvoke = slow_invoke
        mock_orchestrator.compile.return_value = compiled
        app_state.orchestrator = mock_orchestrator

        # 調査を作成
        inv_id = app_state.create_investigation("user_query")
        user_query = UserQuery(raw_input="test query")

        # タイムアウトが発生することを確認
        await _run_user_query_investigation(inv_id, user_query)

        # ステータスがfailedになっている
        record = app_state.get_investigation(inv_id)
        assert record.status == "failed"
        assert "タイムアウト" in record.error


class TestUserInput:
    """ユーザ入力（interrupt resume）エンドポイントのテスト."""

    def test_submit_input_not_found(self, client):
        """存在しない調査IDでは404."""
        response = client.post(
            "/api/v1/investigations/nonexistent/input",
            json={"value": "prom-1"},
        )
        assert response.status_code == 404

    def test_submit_input_not_waiting(self, client):
        """waiting_for_inputでない場合は409."""
        inv_id = app_state.create_investigation("user_query")
        response = client.post(
            f"/api/v1/investigations/{inv_id}/input",
            json={"value": "prom-1"},
        )
        assert response.status_code == 409

    def test_submit_input_success(self, client):
        """正常なresume."""
        inv_id = app_state.create_investigation("user_query")

        # waiting_for_input に設定
        mock_compiled = MagicMock()
        mock_compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        mock_config = {"configurable": {"thread_id": inv_id}}
        app_state.set_waiting_for_input(
            inv_id,
            pending_input={"type": "datasource_selection", "datasource_type": "prometheus"},
            compiled_graph=mock_compiled,
            config=mock_config,
        )

        response = client.post(
            f"/api/v1/investigations/{inv_id}/input",
            json={"value": "prom-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["investigation_id"] == inv_id

    def test_status_includes_pending_input(self, client):
        """waiting_for_input時にpending_inputがステータスに含まれる."""
        inv_id = app_state.create_investigation("user_query")

        mock_compiled = MagicMock()
        mock_config = {"configurable": {"thread_id": inv_id}}
        pending = {
            "type": "datasource_selection",
            "datasource_type": "prometheus",
            "options": [{"uid": "prom-1", "name": "Prometheus 1"}],
        }
        app_state.set_waiting_for_input(
            inv_id,
            pending_input=pending,
            compiled_graph=mock_compiled,
            config=mock_config,
        )

        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "waiting_for_input"
        assert data["pending_input"] is not None
        assert data["pending_input"]["type"] == "datasource_selection"


class TestReportEndpoints:
    """RCAレポート検索・一覧エンドポイントのテスト."""

    def test_reports_list_not_initialized(self, client):
        app_state.report_store = None
        response = client.get("/api/v1/reports")
        assert response.status_code == 503

    def test_reports_list_empty(self, client):
        mock_store = MagicMock()
        mock_store.list_reports.return_value = ([], 0)
        app_state.report_store = mock_store

        response = client.get("/api/v1/reports")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["reports"] == []

    def test_reports_search_not_initialized(self, client):
        app_state.report_search_agent = None
        response = client.post(
            "/api/v1/reports/search",
            json={"query": "OOMKill"},
        )
        assert response.status_code == 503

    def test_reports_search_valid(self, client):
        mock_agent = MagicMock()
        mock_agent.search_and_answer = AsyncMock(
            return_value=ReportSearchResponse(
                answer="テスト回答",
                results=[],
                total_reports=0,
            )
        )
        app_state.report_search_agent = mock_agent

        response = client.post(
            "/api/v1/reports/search",
            json={"query": "OOMKillの原因"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["answer"] == "テスト回答"

    def test_reports_get_not_found(self, client):
        mock_store = MagicMock()
        mock_store.get_report.return_value = None
        app_state.report_store = mock_store

        response = client.get("/api/v1/reports/nonexistent")
        assert response.status_code == 404

    def test_reports_search_empty_query(self, client):
        response = client.post(
            "/api/v1/reports/search",
            json={"query": ""},
        )
        assert response.status_code == 422


class TestReportSearchTimeout:
    """report_search がタイムアウトした場合のフォールバックテスト."""

    def test_report_search_timeout_falls_back_to_investigation(self, client):
        """report_search が遅い場合、タイムアウトして新規調査にフォールバック."""
        import asyncio

        mock_store = MagicMock()
        mock_store.count.return_value = 3

        async def slow_search(**kwargs):
            await asyncio.sleep(60)  # タイムアウトより長い

        mock_search_agent = MagicMock()
        mock_search_agent.search_and_answer = slow_search

        app_state.report_store = mock_store
        app_state.report_search_agent = mock_search_agent

        # "search" を返すLLM
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content="search")
        mock_orchestrator = MagicMock()
        mock_orchestrator.llm = mock_llm
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        mock_orchestrator.compile.return_value = compiled
        app_state.orchestrator = mock_orchestrator

        response = client.post(
            "/api/v1/query",
            json={"query": "前回の問題について教えてください"},
        )
        assert response.status_code == 200
        data = response.json()
        # タイムアウト後、新規調査にフォールバック
        assert data["routed_to"] == "investigation"
        assert data["status"] == "running"

        # cleanup
        app_state.report_store = None
        app_state.report_search_agent = None


class TestSecondQueryAfterReport:
    """1回目の調査完了後に2回目のクエリが正常動作するE2Eフローテスト."""

    def test_second_query_after_completed_investigation(self, client):
        """RCA完了後の2回目のクエリが正常にレスポンスを返す."""
        # --- 1回目: 調査完了 ---
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled
        app_state.report_store = None  # レポートストアなし → report_search スキップ

        resp1 = client.post(
            "/api/v1/query",
            json={"query": "CPU使用率が高い原因を調査して"},
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        inv_id_1 = data1["investigation_id"]
        assert data1["status"] == "running"

        # 調査完了をシミュレート
        report = RCAReport(
            trigger_type=TriggerType.USER_QUERY,
            root_causes=[RootCause(description="CPU spike in app pod", confidence=0.9)],
            markdown="# RCA Report\nCPU spike detected.",
        )
        app_state.complete_investigation(inv_id_1, rca_report=report)

        # レポート取得
        report_resp = client.get(f"/api/v1/investigations/{inv_id_1}/report")
        assert report_resp.status_code == 200
        assert report_resp.json()["markdown"] == "# RCA Report\nCPU spike detected."

        # --- 2回目: 新しいクエリ ---
        resp2 = client.post(
            "/api/v1/query",
            json={"query": "メモリ使用量も確認してください"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "running"
        assert data2["investigation_id"] != inv_id_1

    def test_second_query_with_report_search(self, client):
        """1回目完了後、2回目が report_search にルーティングされても正常レスポンス."""
        # --- 1回目: 調査完了 ---
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled
        app_state.report_store = None

        resp1 = client.post(
            "/api/v1/query",
            json={"query": "クラスタの状態を確認して"},
        )
        assert resp1.status_code == 200
        inv_id_1 = resp1.json()["investigation_id"]

        report = RCAReport(
            trigger_type=TriggerType.USER_QUERY,
            root_causes=[RootCause(description="OOMKilled pod", confidence=0.85)],
            markdown="# Report\nOOMKilled detected.",
        )
        app_state.complete_investigation(inv_id_1, rca_report=report)

        # --- 2回目: report_search で即時完了 ---
        mock_store = MagicMock()
        mock_store.count.return_value = 1
        mock_search_agent = AsyncMock()
        mock_search_agent.search_and_answer.return_value = ReportSearchResponse(
            answer="前回の調査では OOMKilled が検出されました。",
            results=[],
            total_reports=1,
        )
        app_state.report_store = mock_store
        app_state.report_search_agent = mock_search_agent

        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(content="search")
        app_state.orchestrator.llm = mock_llm

        resp2 = client.post(
            "/api/v1/query",
            json={"query": "前回の問題は何でしたか？"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["routed_to"] == "report_search"
        assert data2["status"] == "completed"
        assert "OOMKilled" in data2["report_search_answer"]

        # cleanup
        app_state.report_store = None
        app_state.report_search_agent = None


class TestPendingInputStringType:
    """pending_input が文字列（時間範囲 interrupt）の場合のテスト."""

    def test_status_with_string_pending_input(self, client):
        """pending_input が文字列でも InvestigationStatus のバリデーションが通る."""
        inv_id = app_state.create_investigation("user_query")

        mock_compiled = MagicMock()
        mock_config = {"configurable": {"thread_id": inv_id}}
        app_state.set_waiting_for_input(
            inv_id,
            pending_input="調査対象の時間範囲を教えてください。例: 直近1時間",
            compiled_graph=mock_compiled,
            config=mock_config,
        )

        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "waiting_for_input"
        assert data["pending_input"] == "調査対象の時間範囲を教えてください。例: 直近1時間"

    def test_resume_with_string_pending_input(self, client):
        """文字列 pending_input の調査を resume できる."""
        inv_id = app_state.create_investigation("user_query")

        mock_compiled = MagicMock()
        mock_compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        mock_config = {"configurable": {"thread_id": inv_id}}
        app_state.set_waiting_for_input(
            inv_id,
            pending_input="時間範囲を指定してください",
            compiled_graph=mock_compiled,
            config=mock_config,
        )

        response = client.post(
            f"/api/v1/investigations/{inv_id}/input",
            json={"value": "直近1時間"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
