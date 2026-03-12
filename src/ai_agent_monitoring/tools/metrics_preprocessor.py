"""Prometheus 時系列データの前処理 — 統計的特徴量への圧縮.

生の [[timestamp, "value"], ...] 形式を統計サマリに変換し、
LLM が時系列の特徴を正確に捉えられるようにする。
外部ライブラリ依存なし（statistics モジュール + pure Python）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SERIES = 10


def preprocess_prometheus_result(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Prometheus応答を統計サマリに変換。非時系列データはそのまま返す.

    tool_resultは_extract_result()の出力形式:
    {"content": [{"type": "text", "text": "..."}]}

    textの中身がPrometheus時系列結果（matrix形式またはGrafana MCP形式）の場合
    統計サマリに変換。それ以外はそのまま返す。
    """
    content = tool_result.get("content", [])
    if not content:
        return tool_result

    new_content = []
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "text"):
            new_content.append(item)
            continue

        text = item.get("text", "")
        parsed = _parse_prometheus_response(text)
        if parsed is None:
            new_content.append(item)
            continue

        results = parsed.get("results", [])
        time_range = parsed.get("time_range", {})

        series_summaries = []
        for series in results[:_MAX_SERIES]:
            labels = series.get("metric", {})
            raw_values = series.get("values", [])
            if not raw_values:
                continue

            timestamps = [float(v[0]) for v in raw_values]
            values = [_safe_float(v[1]) for v in raw_values]

            stats = _compute_statistics(values)
            trend = _compute_trend(timestamps, values)
            anomalies = _detect_anomalies(values)
            summary = _format_series_summary(labels, stats, trend, anomalies, timestamps, values)
            series_summaries.append(summary)

        remaining = len(results) - _MAX_SERIES
        summary_text = _format_prometheus_summary(series_summaries, time_range, remaining)
        new_content.append({"type": "text", "text": summary_text})

    return {"content": new_content}


def _safe_float(v: Any) -> float:
    """文字列または数値を float に変換。NaN/Inf は 0.0 にフォールバック."""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except (ValueError, TypeError):
        return 0.0


def _parse_prometheus_response(text: str) -> dict[str, Any] | None:
    """MCP応答テキストからPrometheus matrix結果をパース.

    以下の形式を認識する:
    - 形式1: {"status":"success","data":{"resultType":"matrix","result":[...]}}
    - 形式2: {"resultType":"matrix","result":[...]}
    - 形式3: {"data":[{"metric":{...},"values":[[ts,val],...]},...]}  (Grafana MCP)

    JSON前後に説明文が含まれる場合の正規表現フォールバック付き。
    """
    # まずそのまま JSON パース
    data = _try_parse_json(text)

    if data is None:
        # 正規表現フォールバック: テキスト中の {...} を抽出
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            data = _try_parse_json(match.group(0))

    if data is None:
        return None

    if not isinstance(data, dict):
        return None

    # 形式1/2: {"data": {"resultType": "matrix", "result": [...]}}
    inner = data.get("data", data)
    if isinstance(inner, dict) and inner.get("resultType") == "matrix":
        result_list = inner.get("result", [])
        time_range = _extract_time_range(result_list)
        return {
            "results": result_list,
            "time_range": time_range,
        }

    # 形式3 (Grafana MCP): {"data": [{"metric": {...}, "values": [[ts, val], ...]}, ...]}
    if isinstance(inner, list) and inner and _is_series_list(inner):
        time_range = _extract_time_range(inner)
        return {
            "results": inner,
            "time_range": time_range,
        }

    return None


def _is_series_list(items: list[Any]) -> bool:
    """リストが Prometheus series の配列かどうかを判定."""
    first = items[0]
    return isinstance(first, dict) and ("values" in first or "value" in first)


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """JSON パースを試行。失敗時は None."""
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_time_range(result_list: list[dict[str, Any]]) -> dict[str, str]:
    """結果リストからタイムレンジを抽出."""
    if not result_list:
        return {}

    all_timestamps: list[float] = []
    for series in result_list:
        values = series.get("values", [])
        if values:
            all_timestamps.append(float(values[0][0]))
            all_timestamps.append(float(values[-1][0]))

    if not all_timestamps:
        return {}

    start_ts = min(all_timestamps)
    end_ts = max(all_timestamps)
    return {
        "start": datetime.fromtimestamp(start_ts, tz=UTC).isoformat(),
        "end": datetime.fromtimestamp(end_ts, tz=UTC).isoformat(),
    }


def _compute_statistics(values: list[float]) -> dict[str, float]:
    """基本統計量を計算: mean, stdev, min, max, p50, p95, p99."""
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "stdev": 0.0, "min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if n >= 2 else 0.0

    if n >= 2:
        quantiles = statistics.quantiles(values, n=100)
        p50 = quantiles[49]
        p95 = quantiles[94]
        p99 = quantiles[98]
    else:
        p50 = p95 = p99 = values[0]

    return {
        "mean": round(mean, 6),
        "stdev": round(stdev, 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "p50": round(p50, 6),
        "p95": round(p95, 6),
        "p99": round(p99, 6),
    }


def _compute_trend(timestamps: list[float], values: list[float]) -> dict[str, Any]:
    """線形回帰スロープ + 変化率を計算."""
    n = len(values)
    if n <= 1:
        return {"slope": 0.0, "direction": "flat", "max_spike": 0.0, "mean_change": 0.0}

    slope = _linear_slope(timestamps, values)

    # 変化率
    changes = [abs(values[i] - values[i - 1]) for i in range(1, n)]
    max_spike = max(changes) if changes else 0.0
    mean_change = statistics.mean(changes) if changes else 0.0

    # 方向判定
    if abs(slope) < 1e-10:
        direction = "flat"
    elif slope > 0:
        direction = "increasing"
    else:
        direction = "decreasing"

    return {
        "slope": round(slope, 10),
        "direction": direction,
        "max_spike": round(max_spike, 6),
        "mean_change": round(mean_change, 6),
    }


def _linear_slope(x: list[float], y: list[float]) -> float:
    """最小二乗法で線形回帰スロープを計算."""
    n = len(x)
    if n <= 1:
        return 0.0

    x_mean = sum(x) / n
    y_mean = sum(y) / n
    num = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y, strict=True))
    den = sum((xi - x_mean) ** 2 for xi in x)
    return num / den if den != 0 else 0.0


def _detect_anomalies(values: list[float], threshold_sigma: float = 3.0) -> dict[str, Any]:
    """z-scoreベースの異常検出."""
    n = len(values)
    if n < 2:
        return {"count": 0, "max_z_score": 0.0, "indices": []}

    mean = statistics.mean(values)
    stdev = statistics.stdev(values)

    if stdev == 0:
        return {"count": 0, "max_z_score": 0.0, "indices": []}

    anomaly_indices: list[int] = []
    max_z = 0.0
    for i, v in enumerate(values):
        z = abs(v - mean) / stdev
        if z > threshold_sigma:
            anomaly_indices.append(i)
        if z > max_z:
            max_z = z

    return {
        "count": len(anomaly_indices),
        "max_z_score": round(max_z, 2),
        "indices": anomaly_indices,
    }


def _format_series_summary(
    labels: dict[str, str],
    stats: dict[str, float],
    trend: dict[str, Any],
    anomalies: dict[str, Any],
    timestamps: list[float],
    values: list[float],
) -> dict[str, Any]:
    """1シリーズの特徴量サマリを構築."""
    # 異常タイムスタンプを人間可読形式に変換
    anomaly_timestamps = []
    for idx in anomalies.get("indices", [])[:5]:  # 最大5件
        if idx < len(timestamps):
            ts = datetime.fromtimestamp(timestamps[idx], tz=UTC)
            anomaly_timestamps.append(ts.isoformat())

    # ラベル文字列を簡潔に
    label_str = json.dumps(labels, ensure_ascii=False, separators=(",", ":")) if labels else "{}"

    return {
        "labels": label_str,
        "n_points": len(values),
        "stats": stats,
        "trend": trend,
        "anomalies": {
            "count": anomalies["count"],
            "max_z_score": anomalies["max_z_score"],
            "timestamps": anomaly_timestamps,
        },
        "first_value": round(values[0], 6) if values else 0.0,
        "last_value": round(values[-1], 6) if values else 0.0,
    }


def _format_prometheus_summary(
    series_summaries: list[dict[str, Any]],
    time_range: dict[str, str],
    remaining_count: int = 0,
) -> str:
    """全シリーズの統合サマリをJSON文字列化."""
    summary: dict[str, Any] = {
        "type": "prometheus_summary",
        "time_range": time_range,
        "series_count": len(series_summaries) + max(0, remaining_count),
        "series": series_summaries,
    }

    if remaining_count > 0:
        summary["truncated"] = f"... and {remaining_count} more series"

    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
