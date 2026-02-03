"""Time Tool — 現在時刻取得ツール.

LLMが現在時刻を認識できるようにするためのローカルツール。
MCPサーバーは不要で、サーバーのシステム時刻を返す。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)

# デフォルトのタイムゾーン（日本時間）
DEFAULT_TIMEZONE = "Asia/Tokyo"


def get_current_time(tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    """指定タイムゾーンでの現在時刻を取得."""
    try:
        tz: ZoneInfo | timezone = ZoneInfo(tz_name)
    except KeyError:
        logger.warning("Unknown timezone '%s', falling back to UTC", tz_name)
        tz = timezone.utc
    return datetime.now(tz)


def create_time_tools(default_tz: str = DEFAULT_TIMEZONE) -> list[BaseTool]:
    """時刻関連のLangChain Toolを生成."""

    @tool
    def get_current_datetime(timezone_name: str = default_tz) -> dict[str, Any]:
        """現在の日時を取得します。

        Args:
            timezone_name: タイムゾーン名（例: Asia/Tokyo, UTC, America/New_York）

        Returns:
            現在時刻の情報（ISO 8601形式、Unix timestamp、人間可読形式）
        """
        now = get_current_time(timezone_name)
        return {
            "iso8601": now.isoformat(),
            "unix_timestamp": int(now.timestamp()),
            "human_readable": now.strftime("%Y年%m月%d日 %H時%M分%S秒"),
            "timezone": timezone_name,
            "utc_offset": now.strftime("%z"),
        }

    @tool
    def calculate_time_range(
        duration_minutes: int = 30,
        end_time: str = "",
        timezone_name: str = default_tz,
    ) -> dict[str, str]:
        """指定した期間の時間範囲を計算します。

        Args:
            duration_minutes: 期間（分）。正の値で過去N分間を計算。
            end_time: 終了時刻（ISO 8601形式）。空の場合は現在時刻を使用。
            timezone_name: タイムゾーン名

        Returns:
            start/end をISO 8601形式で返す
        """
        if end_time:
            try:
                end = datetime.fromisoformat(end_time)
            except ValueError:
                end = get_current_time(timezone_name)
        else:
            end = get_current_time(timezone_name)

        start = end - timedelta(minutes=duration_minutes)

        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_minutes": str(duration_minutes),
        }

    @tool
    def parse_relative_time(
        expression: str,
        timezone_name: str = default_tz,
    ) -> dict[str, str]:
        """相対的な時間表現をISO 8601形式に変換します。

        Args:
            expression: 時間表現（例: "30分前", "1時間前", "昨日", "今日の15時"）
            timezone_name: タイムゾーン名

        Returns:
            解釈した時刻をISO 8601形式で返す

        Note:
            このツールは基本的な相対時間のみ対応。複雑な表現はLLMが解釈してください。
        """
        now = get_current_time(timezone_name)
        result_time = now

        # 基本的なパターンマッチング
        if "分前" in expression:
            try:
                minutes = int("".join(filter(str.isdigit, expression)))
                result_time = now - timedelta(minutes=minutes)
            except ValueError:
                pass
        elif "時間前" in expression:
            try:
                hours = int("".join(filter(str.isdigit, expression)))
                result_time = now - timedelta(hours=hours)
            except ValueError:
                pass
        elif "昨日" in expression:
            result_time = now - timedelta(days=1)
        elif "一昨日" in expression or "おととい" in expression:
            result_time = now - timedelta(days=2)

        return {
            "original_expression": expression,
            "interpreted_time": result_time.isoformat(),
            "current_time": now.isoformat(),
            "timezone": timezone_name,
        }

    return [get_current_datetime, calculate_time_range, parse_relative_time]
