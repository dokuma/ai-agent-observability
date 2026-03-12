"""metrics_preprocessor のテスト."""

from __future__ import annotations

import json

import pytest

from ai_agent_monitoring.tools.metrics_preprocessor import (
    _compute_statistics,
    _compute_trend,
    _detect_anomalies,
    _parse_prometheus_response,
    preprocess_prometheus_result,
)


class TestParsePrometheusResponse:
    """_parse_prometheus_response のテスト."""

    def test_parse_matrix_result(self):
        """matrix結果を正しくパースできる."""
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"instance": "node1"},
                        "values": [[1700000000, "0.85"], [1700000060, "0.87"]],
                    }
                ],
            },
        }
        text = json.dumps(data)
        parsed = _parse_prometheus_response(text)

        assert parsed is not None
        assert len(parsed["results"]) == 1
        assert parsed["results"][0]["metric"]["instance"] == "node1"

    def test_parse_vector_result_returns_none(self):
        """vector結果はNoneを返す."""
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"instance": "node1"}, "value": [1700000000, "0.85"]}],
            },
        }
        text = json.dumps(data)
        assert _parse_prometheus_response(text) is None

    def test_parse_invalid_json_returns_none(self):
        """不正JSONはNoneを返す."""
        assert _parse_prometheus_response("not json at all") is None

    def test_parse_empty_result_array(self):
        """空のresult配列."""
        data = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        text = json.dumps(data)
        parsed = _parse_prometheus_response(text)

        assert parsed is not None
        assert parsed["results"] == []

    def test_parse_with_surrounding_text(self):
        """JSON前後に説明文が含まれる場合のフォールバック."""
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {}, "values": [[1700000000, "1.0"]]}],
            },
        }
        text = f"Here is the result: {json.dumps(data)} end of response"
        parsed = _parse_prometheus_response(text)

        assert parsed is not None
        assert len(parsed["results"]) == 1

    def test_parse_nested_data_without_outer_wrapper(self):
        """data直接のフォーマット（外側のstatus/dataラッパーなし）."""
        data = {
            "resultType": "matrix",
            "result": [{"metric": {"job": "test"}, "values": [[1700000000, "0.5"]]}],
        }
        text = json.dumps(data)
        parsed = _parse_prometheus_response(text)

        assert parsed is not None
        assert len(parsed["results"]) == 1


class TestComputeStatistics:
    """_compute_statistics のテスト."""

    def test_known_data(self):
        """既知データの統計量を検証."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        stats = _compute_statistics(values)

        assert stats["mean"] == pytest.approx(5.5, abs=0.01)
        assert stats["min"] == 1.0
        assert stats["max"] == 10.0
        assert stats["stdev"] > 0

    def test_single_element(self):
        """1要素の場合."""
        stats = _compute_statistics([42.0])

        assert stats["mean"] == 42.0
        assert stats["stdev"] == 0.0
        assert stats["p50"] == 42.0
        assert stats["p95"] == 42.0

    def test_all_same_values(self):
        """全同値の場合 stdev=0."""
        values = [5.0] * 20
        stats = _compute_statistics(values)

        assert stats["mean"] == 5.0
        assert stats["stdev"] == 0.0
        assert stats["min"] == 5.0
        assert stats["max"] == 5.0

    def test_empty_values(self):
        """空の場合."""
        stats = _compute_statistics([])
        assert stats["mean"] == 0.0


class TestComputeTrend:
    """_compute_trend のテスト."""

    def test_monotone_increasing(self):
        """単調増加→正slope."""
        timestamps = [float(i) for i in range(10)]
        values = [float(i) for i in range(10)]
        trend = _compute_trend(timestamps, values)

        assert trend["slope"] > 0
        assert trend["direction"] == "increasing"

    def test_monotone_decreasing(self):
        """単調減少→負slope."""
        timestamps = [float(i) for i in range(10)]
        values = [10.0 - float(i) for i in range(10)]
        trend = _compute_trend(timestamps, values)

        assert trend["slope"] < 0
        assert trend["direction"] == "decreasing"

    def test_flat(self):
        """横ばい→≈0."""
        timestamps = [float(i) for i in range(10)]
        values = [5.0] * 10
        trend = _compute_trend(timestamps, values)

        assert abs(trend["slope"]) < 1e-9
        assert trend["direction"] == "flat"

    def test_single_point(self):
        """1点のみ."""
        trend = _compute_trend([1.0], [5.0])

        assert trend["slope"] == 0.0
        assert trend["direction"] == "flat"


class TestDetectAnomalies:
    """_detect_anomalies のテスト."""

    def test_spike_data(self):
        """スパイクを含むデータ."""
        # 正常値は1.0付近、スパイクは100.0
        values = [1.0] * 50 + [100.0] + [1.0] * 49
        anomalies = _detect_anomalies(values)

        assert anomalies["count"] >= 1
        assert anomalies["max_z_score"] > 3.0
        assert 50 in anomalies["indices"]

    def test_no_anomalies(self):
        """異常なしデータ."""
        values = [1.0, 1.01, 0.99, 1.02, 0.98, 1.0, 1.01, 0.99, 1.0, 1.0]
        anomalies = _detect_anomalies(values)

        assert anomalies["count"] == 0

    def test_custom_threshold(self):
        """閾値変更."""
        values = [1.0] * 20 + [3.0]  # mild spike
        # 低い閾値で検出
        anomalies_low = _detect_anomalies(values, threshold_sigma=1.0)
        # 高い閾値で未検出
        anomalies_high = _detect_anomalies(values, threshold_sigma=10.0)

        assert anomalies_low["count"] >= anomalies_high["count"]

    def test_single_value(self):
        """1要素の場合."""
        anomalies = _detect_anomalies([5.0])
        assert anomalies["count"] == 0

    def test_all_same_values(self):
        """全同値: stdev=0 → 異常なし."""
        anomalies = _detect_anomalies([5.0] * 20)
        assert anomalies["count"] == 0


class TestPreprocessPrometheusResult:
    """preprocess_prometheus_result の統合テスト."""

    def _make_matrix_result(self, values: list[tuple[float, str]], metric: dict | None = None) -> dict:
        """テスト用のmatrix形式ツール結果を生成."""
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": metric or {"instance": "node1"},
                        "values": [[ts, val] for ts, val in values],
                    }
                ],
            },
        }
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    def test_timeseries_to_summary(self):
        """時系列データ→サマリ変換."""
        values = [(1700000000 + i * 60, str(0.85 + i * 0.001)) for i in range(60)]
        tool_result = self._make_matrix_result(values)

        result = preprocess_prometheus_result(tool_result)

        assert len(result["content"]) == 1
        summary = json.loads(result["content"][0]["text"])
        assert summary["type"] == "prometheus_summary"
        assert summary["series_count"] == 1
        assert len(summary["series"]) == 1

        series = summary["series"][0]
        assert series["n_points"] == 60
        assert "mean" in series["stats"]
        assert "slope" in series["trend"]
        assert "count" in series["anomalies"]

    def test_non_timeseries_passthrough(self):
        """非時系列データはスルー."""
        tool_result = {"content": [{"type": "text", "text": '{"some": "data"}'}]}
        result = preprocess_prometheus_result(tool_result)
        assert result == tool_result

    def test_empty_content(self):
        """空contentはスルー."""
        tool_result = {"content": []}
        result = preprocess_prometheus_result(tool_result)
        assert result == tool_result

    def test_vector_result_passthrough(self):
        """vector結果はスルー."""
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"instance": "node1"}, "value": [1700000000, "0.85"]}],
            },
        }
        tool_result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        result = preprocess_prometheus_result(tool_result)
        assert result == tool_result

    def test_summary_fits_in_truncation_limit(self):
        """サマリがtruncation limit（8000文字）に収まる."""
        values = [(1700000000 + i * 15, str(0.5 + (i % 10) * 0.05)) for i in range(240)]
        tool_result = self._make_matrix_result(values)

        result = preprocess_prometheus_result(tool_result)

        summary_text = result["content"][0]["text"]
        assert len(summary_text) < 8000

    def test_multiple_series(self):
        """複数シリーズの処理."""
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"instance": f"node{i}"},
                        "values": [[1700000000 + j * 60, str(0.5 + j * 0.01)] for j in range(10)],
                    }
                    for i in range(3)
                ],
            },
        }
        tool_result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        result = preprocess_prometheus_result(tool_result)

        summary = json.loads(result["content"][0]["text"])
        assert summary["series_count"] == 3
        assert len(summary["series"]) == 3

    def test_series_truncation(self):
        """11シリーズ以上の場合、上位10件に制限."""
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"instance": f"node{i}"},
                        "values": [[1700000000, str(i * 0.1)]],
                    }
                    for i in range(15)
                ],
            },
        }
        tool_result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        result = preprocess_prometheus_result(tool_result)

        summary = json.loads(result["content"][0]["text"])
        assert len(summary["series"]) == 10
        assert summary["series_count"] == 15
        assert "truncated" in summary

    def test_non_text_content_preserved(self):
        """非テキストコンテンツはそのまま保持."""
        tool_result = {
            "content": [
                {"type": "image", "data": "base64data"},
                {"type": "text", "text": "plain text"},
            ]
        }
        result = preprocess_prometheus_result(tool_result)
        assert result["content"][0] == {"type": "image", "data": "base64data"}
        assert result["content"][1] == {"type": "text", "text": "plain text"}
