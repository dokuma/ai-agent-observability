"""Pipe Function (docs/integration.md) のテスト.

docs/integration.md 内のコードブロックから Pipe クラスの
ロジックをテストする。Open WebUI 依存のない純粋なユニットテスト。
"""

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---- Pipe クラスを docs/integration.md から動的にロード ----

_PIPE_MODULE = None


def _load_pipe_class():
    """integration.md 内の Python コードブロックから Pipe クラスをロード."""
    global _PIPE_MODULE
    if _PIPE_MODULE is not None:
        return _PIPE_MODULE

    md_path = Path(__file__).parent.parent / "docs" / "integration.md"
    content = md_path.read_text()

    # ```python ... ``` ブロックを抽出
    match = re.search(r"```python\n(.*?)```", content, re.DOTALL)
    assert match, "integration.md に Python コードブロックが見つかりません"

    code = match.group(1)
    module_dict: dict[str, Any] = {}
    exec(compile(code, "integration.md", "exec"), module_dict)  # noqa: S102

    _PIPE_MODULE = module_dict
    return module_dict


def _make_pipe():
    """Pipe インスタンスを生成."""
    module = _load_pipe_class()
    pipe_cls = module["Pipe"]
    pipe = pipe_cls()
    # テスト用に短い間隔に設定
    pipe.valves.POLL_INTERVAL = 0
    pipe.valves.POLL_TIMEOUT = 10
    return pipe


# ---- _format_input_request テスト ----


class TestFormatInputRequest:
    """_format_input_request のテスト."""

    def test_none_pending_input(self):
        """pending_input が None の場合、汎用メッセージを返す."""
        pipe = _make_pipe()
        result = pipe._format_input_request("inv-123", None)
        assert "調査を続行するために入力が必要です" in result
        assert "<!-- investigation_id: inv-123 -->" in result

    def test_datasource_selection(self):
        """datasource_selection タイプでオプション一覧が表示される."""
        pipe = _make_pipe()
        pending = {
            "type": "datasource_selection",
            "datasource_type": "prometheus",
            "message": "使用するprometheusデータソースを選択してください:",
            "options": [
                {"uid": "prom-1", "name": "Prometheus 1", "is_default": False, "recommended": False},
                {"uid": "prom-2", "name": "Prometheus 2", "is_default": True, "recommended": True},
            ],
        }
        result = pipe._format_input_request("inv-456", pending)

        assert "使用するprometheusデータソースを選択してください:" in result
        assert "**Prometheus 1**" in result
        assert "(`prom-1`)" in result
        assert "**Prometheus 2**" in result
        assert "(`prom-2`)" in result
        assert "<!-- investigation_id: inv-456 -->" in result

    def test_datasource_selection_recommended_star(self):
        """recommended フラグがある候補に星マーカーが付く."""
        pipe = _make_pipe()
        pending = {
            "type": "datasource_selection",
            "datasource_type": "prometheus",
            "message": "選択してください:",
            "options": [
                {"uid": "prom-1", "name": "DS A", "recommended": False},
                {"uid": "prom-2", "name": "DS B", "recommended": True},
            ],
        }
        result = pipe._format_input_request("inv-1", pending)

        lines = result.split("\n")
        ds_a_line = next(line for line in lines if "DS A" in line)
        ds_b_line = next(line for line in lines if "DS B" in line)
        assert "⭐" not in ds_a_line
        assert "⭐" in ds_b_line

    def test_datasource_retry(self):
        """datasource_retry タイプでエラーと代替候補が表示される."""
        pipe = _make_pipe()
        pending = {
            "type": "datasource_retry",
            "datasource_type": "prometheus",
            "failed_uid": "prom-1",
            "error": "Connection refused",
            "message": "別のデータソースを選択してください:",
            "options": [
                {"uid": "prom-2", "name": "Prometheus 2", "is_default": False},
            ],
        }
        result = pipe._format_input_request("inv-789", pending)

        assert "別のデータソースを選択してください:" in result
        assert "`Connection refused`" in result
        assert "**Prometheus 2**" in result
        assert "<!-- investigation_id: inv-789 -->" in result

    def test_generic_input(self):
        """不明なタイプの場合、汎用フォーマットで返す."""
        pipe = _make_pipe()
        pending = {
            "type": "time_range",
            "message": "調査対象の時間範囲を指定してください",
        }
        result = pipe._format_input_request("inv-000", pending)

        assert "調査対象の時間範囲を指定してください" in result
        assert "<!-- investigation_id: inv-000 -->" in result


# ---- _try_resume テスト ----


class TestTryResume:
    """_try_resume のテスト."""

    @pytest.mark.asyncio
    async def test_no_messages(self):
        """メッセージが不足している場合は None."""
        pipe = _make_pipe()
        result = await pipe._try_resume({"messages": []}, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_marker_in_assistant(self):
        """アシスタントメッセージにマーカーがない場合は None."""
        pipe = _make_pipe()
        body = {
            "messages": [
                {"role": "assistant", "content": "普通の応答"},
                {"role": "user", "content": "次の質問"},
            ]
        }
        result = await pipe._try_resume(body, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_resume_with_marker(self):
        """マーカーがある場合、API に resume リクエストを送る."""
        pipe = _make_pipe()
        inv_id = "test-inv-123"
        marker = f"<!-- investigation_id: {inv_id} -->"

        body = {
            "messages": [
                {"role": "assistant", "content": f"データソースを選択してください\n{marker}"},
                {"role": "user", "content": "1"},
            ]
        }

        mock_post_response = MagicMock()
        mock_post_response.status_code = 200
        mock_post_response.raise_for_status = MagicMock()

        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {"status": "completed"}

        mock_report_response = MagicMock()
        mock_report_response.status_code = 200
        mock_report_response.json.return_value = {"markdown": "# レポート"}

        with patch("requests.post", return_value=mock_post_response) as mock_post, patch("requests.get") as mock_get:
            mock_get.side_effect = [mock_get_response, mock_report_response]
            result = await pipe._try_resume(body, None)

        # resume API が呼ばれたことを確認
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert f"/investigations/{inv_id}/input" in call_url
        call_json = mock_post.call_args[1]["json"]
        assert call_json["value"] == "1"

        assert result is not None  # None でないこと（resume が処理された）

    @pytest.mark.asyncio
    async def test_resume_409_falls_through(self):
        """409 応答の場合は None を返して通常クエリに移行."""
        pipe = _make_pipe()
        inv_id = "test-inv-409"
        marker = f"<!-- investigation_id: {inv_id} -->"

        body = {
            "messages": [
                {"role": "assistant", "content": f"選択してください\n{marker}"},
                {"role": "user", "content": "1"},
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 409

        with patch("requests.post", return_value=mock_response):
            result = await pipe._try_resume(body, None)

        assert result is None


# ---- _poll_until_done テスト ----


class TestPollUntilDone:
    """_poll_until_done のテスト."""

    @pytest.mark.asyncio
    async def test_waiting_for_input_returns_selection(self):
        """ポーリング中に waiting_for_input を検出したらデータソース選択を返す."""
        pipe = _make_pipe()

        pending_input = {
            "type": "datasource_selection",
            "datasource_type": "prometheus",
            "message": "選択してください:",
            "options": [
                {"uid": "prom-1", "name": "Prometheus 1", "recommended": True},
                {"uid": "prom-2", "name": "Prometheus 2", "recommended": False},
            ],
        }

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "waiting_for_input",
            "pending_input": pending_input,
        }

        emitted = []

        async def mock_emitter(event):
            emitted.append(event)

        with patch("requests.get", return_value=mock_response):
            result = await pipe._poll_until_done("inv-poll", "http://test:8000/api/v1", mock_emitter)

        # データソース選択フォーマットが返されること
        assert "選択してください:" in result
        assert "**Prometheus 1**" in result
        assert "**Prometheus 2**" in result
        assert "<!-- investigation_id: inv-poll -->" in result
        # 星マーカー
        assert "⭐" in result

    @pytest.mark.asyncio
    async def test_completed_returns_report(self):
        """completed 検出後にレポートを返す."""
        pipe = _make_pipe()

        status_response = MagicMock()
        status_response.json.return_value = {"status": "completed"}

        report_response = MagicMock()
        report_response.status_code = 200
        report_response.json.return_value = {"markdown": "# RCA Report"}

        with patch("requests.get", side_effect=[status_response, report_response]):
            result = await pipe._poll_until_done("inv-done", "http://test:8000/api/v1", None)

        assert result == "# RCA Report"

    @pytest.mark.asyncio
    async def test_failed_returns_error(self):
        """failed 検出時にエラーメッセージを返す."""
        pipe = _make_pipe()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "failed",
            "error": "MCP connection failed",
        }

        with patch("requests.get", return_value=mock_response):
            result = await pipe._poll_until_done("inv-fail", "http://test:8000/api/v1", None)

        assert "MCP connection failed" in result


# ---- _check_interrupt テスト (API 側) ----


class TestCheckInterrupt:
    """routes._check_interrupt のテスト."""

    def test_interrupt_detected(self):
        """__interrupt__ キーを検出して set_waiting_for_input が呼ばれる."""
        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "datasource_selection",
            "datasource_type": "prometheus",
            "options": [{"uid": "prom-1", "name": "Prometheus"}],
        }

        result = {
            "environment": {},
            "__interrupt__": [mock_interrupt],
        }

        mock_compiled = MagicMock()
        mock_config = {"configurable": {"thread_id": "inv-1"}}

        with patch("ai_agent_monitoring.api.routes.app_state") as mock_app:
            from ai_agent_monitoring.api.routes import _check_interrupt

            found = _check_interrupt(result, "inv-1", mock_compiled, mock_config)

        assert found is True
        mock_app.set_waiting_for_input.assert_called_once_with(
            "inv-1",
            pending_input=mock_interrupt.value,
            compiled_graph=mock_compiled,
            config=mock_config,
        )

    def test_no_interrupt(self):
        """__interrupt__ キーがない場合は False."""
        result = {"environment": {}, "rca_report": None}

        with patch("ai_agent_monitoring.api.routes.app_state"):
            from ai_agent_monitoring.api.routes import _check_interrupt

            found = _check_interrupt(result, "inv-no", MagicMock(), {})

        assert found is False
