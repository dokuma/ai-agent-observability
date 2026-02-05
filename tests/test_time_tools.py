"""time tools のテスト."""

from datetime import datetime, timedelta, timezone

from ai_agent_monitoring.tools.time import (
    create_time_tools,
    get_current_time,
)


class TestGetCurrentTime:
    """get_current_time のテスト."""

    def test_default_timezone(self):
        """デフォルト（日本時間）で現在時刻を取得."""
        result = get_current_time()
        assert result.tzinfo is not None
        # 日本時間のオフセットは+9時間
        assert result.utcoffset() == timedelta(hours=9)

    def test_utc_timezone(self):
        """UTCで現在時刻を取得."""
        result = get_current_time("UTC")
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_invalid_timezone_fallback(self):
        """無効なタイムゾーンの場合UTCにフォールバック."""
        result = get_current_time("Invalid/Timezone")
        assert result.tzinfo == timezone.utc


class TestCreateTimeTools:
    """create_time_tools のテスト."""

    def test_creates_three_tools(self):
        """3つのツールが生成される."""
        tools = create_time_tools()
        assert len(tools) == 3

        tool_names = [t.name for t in tools]
        assert "get_current_datetime" in tool_names
        assert "calculate_time_range" in tool_names
        assert "parse_relative_time" in tool_names


class TestGetCurrentDatetimeTool:
    """get_current_datetime ツールのテスト."""

    def setup_method(self):
        self.tools = create_time_tools()
        self.tool = next(t for t in self.tools if t.name == "get_current_datetime")

    def test_returns_required_fields(self):
        """必須フィールドが返される."""
        result = self.tool.invoke({})
        assert "iso8601" in result
        assert "unix_timestamp" in result
        assert "human_readable" in result
        assert "timezone" in result
        assert "utc_offset" in result

    def test_iso8601_format(self):
        """ISO 8601形式で返される."""
        result = self.tool.invoke({})
        # パースできることを確認
        datetime.fromisoformat(result["iso8601"])

    def test_custom_timezone(self):
        """カスタムタイムゾーンを指定."""
        result = self.tool.invoke({"timezone_name": "UTC"})
        assert result["timezone"] == "UTC"
        assert result["utc_offset"] == "+0000"


class TestCalculateTimeRangeTool:
    """calculate_time_range ツールのテスト."""

    def setup_method(self):
        self.tools = create_time_tools()
        self.tool = next(t for t in self.tools if t.name == "calculate_time_range")

    def test_default_30_minutes(self):
        """デフォルトで30分間の範囲."""
        result = self.tool.invoke({})
        assert result["duration_minutes"] == "30"

        start = datetime.fromisoformat(result["start"])
        end = datetime.fromisoformat(result["end"])
        diff = end - start
        assert diff == timedelta(minutes=30)

    def test_custom_duration(self):
        """カスタム期間を指定."""
        result = self.tool.invoke({"duration_minutes": 60})
        assert result["duration_minutes"] == "60"

        start = datetime.fromisoformat(result["start"])
        end = datetime.fromisoformat(result["end"])
        diff = end - start
        assert diff == timedelta(minutes=60)

    def test_custom_end_time(self):
        """終了時刻を指定."""
        end_time = "2026-02-05T12:00:00+09:00"
        result = self.tool.invoke({"end_time": end_time, "duration_minutes": 30})

        end = datetime.fromisoformat(result["end"])
        start = datetime.fromisoformat(result["start"])

        assert end.isoformat() == end_time
        assert (end - start) == timedelta(minutes=30)

    def test_invalid_end_time_fallback(self):
        """無効な終了時刻は現在時刻にフォールバック."""
        result = self.tool.invoke({"end_time": "invalid-time"})
        # エラーにならず結果が返ることを確認
        assert "start" in result
        assert "end" in result


class TestParseRelativeTimeTool:
    """parse_relative_time ツールのテスト."""

    def setup_method(self):
        self.tools = create_time_tools()
        self.tool = next(t for t in self.tools if t.name == "parse_relative_time")

    def test_minutes_ago(self):
        """「N分前」の解釈."""
        result = self.tool.invoke({"expression": "30分前"})

        assert result["original_expression"] == "30分前"
        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        diff = current - interpreted
        assert diff == timedelta(minutes=30)

    def test_hours_ago(self):
        """「N時間前」の解釈."""
        result = self.tool.invoke({"expression": "2時間前"})

        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        diff = current - interpreted
        assert diff == timedelta(hours=2)

    def test_yesterday(self):
        """「昨日」の解釈."""
        result = self.tool.invoke({"expression": "昨日"})

        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        diff = current - interpreted
        assert diff == timedelta(days=1)

    def test_day_before_yesterday(self):
        """「一昨日」の解釈."""
        result = self.tool.invoke({"expression": "一昨日"})

        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        diff = current - interpreted
        assert diff == timedelta(days=2)

    def test_ototoi(self):
        """「おととい」の解釈."""
        result = self.tool.invoke({"expression": "おととい"})

        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        diff = current - interpreted
        assert diff == timedelta(days=2)

    def test_unknown_expression(self):
        """未知の表現は現在時刻を返す."""
        result = self.tool.invoke({"expression": "来週の月曜日"})

        current = datetime.fromisoformat(result["current_time"])
        interpreted = datetime.fromisoformat(result["interpreted_time"])
        # 未知の表現は現在時刻と同じ
        assert current == interpreted

    def test_custom_timezone(self):
        """タイムゾーンを指定."""
        result = self.tool.invoke({
            "expression": "30分前",
            "timezone_name": "UTC",
        })
        assert result["timezone"] == "UTC"
