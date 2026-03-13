"""JSON 抽出・修復ユーティリティのテスト."""

import json

import pytest

from ai_agent_monitoring.core.json_repair import (
    extract_json,
    repair_truncated_json,
    strip_json_comments,
)


class TestExtractJson:
    def test_code_block(self):
        text = '```json\n{"key": "value"}\n```'
        result = extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_code_block_no_closing(self):
        text = '```json\n{"key": "value"}'
        result = extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_generic_code_block(self):
        text = '```\n{"key": "value"}\n```'
        result = extract_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_inline(self):
        text = 'result: {"a": 1}'
        result = extract_json(text)
        assert json.loads(result) == {"a": 1}

    def test_no_closing_brace(self):
        """閉じ } がない場合、{ 以降を返す."""
        text = 'prefix {"key": "val'
        result = extract_json(text)
        assert result == '{"key": "val'

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            extract_json("no json here")

    def test_surrounding_text(self):
        text = 'Here is the result:\n{"root_causes": []}\nEnd of output.'
        result = extract_json(text)
        assert json.loads(result) == {"root_causes": []}

    def test_embedded_code_block_in_json_string(self):
        """JSON値内に ```promql ``` コードブロックが含まれる場合."""
        text = (
            "```json\n"
            "{\n"
            '  "root_causes": [{"description": "CPU高負荷", "confidence": 0.9, "evidence": ["spike"]}],\n'
            '  "metrics_summary": "CPU分析:\\n```promql\\nrate(cpu[5m])\\n```\\nmax=95%",\n'
            '  "recommendations": ["スケールアウト"]\n'
            "}\n"
            "```"
        )
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["root_causes"][0]["description"] == "CPU高負荷"
        assert "```promql" in parsed["metrics_summary"]

    def test_multiple_embedded_code_blocks(self):
        """JSON値内に複数のコードブロックが含まれる場合."""
        text = (
            "```json\n"
            "{\n"
            '  "metrics_summary": "```promql\\nrate(cpu[5m])\\n```\\nand\\n```logql\\n{app=\\"x\\"}\\n```",\n'
            '  "root_causes": []\n'
            "}\n"
            "```"
        )
        result = extract_json(text)
        parsed = json.loads(result)
        assert "```promql" in parsed["metrics_summary"]
        assert "```logql" in parsed["metrics_summary"]


class TestRepairTruncatedJson:
    def test_unclosed_object(self):
        text = '{"key": "value"'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_unclosed_array(self):
        text = '{"items": [1, 2, 3'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"items": [1, 2, 3]}

    def test_unclosed_string(self):
        text = '{"key": "val'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"key": "val"}

    def test_trailing_comma(self):
        text = '{"a": 1, "b": 2,}'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        text = '{"items": [1, 2,]}'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"items": [1, 2]}

    def test_complete_json_unchanged(self):
        text = '{"key": "value"}'
        result = repair_truncated_json(text)
        assert json.loads(result) == {"key": "value"}

    def test_nested_truncation(self):
        text = '{"root_causes": [{"description": "OOM", "confidence": 0.9'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["root_causes"][0]["description"] == "OOM"

    def test_trailing_colon(self):
        """キー名の後にコロンだけで値がない場合."""
        text = '{"key": "value", "next":'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_line_comments(self):
        """行コメント (//) が含まれるJSONの修復."""
        text = '{\n  "key": "value", // これはコメント\n  "num": 42\n}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value", "num": 42}

    def test_block_comments(self):
        """ブロックコメント (/* */) が含まれるJSONの修復."""
        text = '{\n  /* メトリクス */\n  "key": "value"\n}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_comments_and_trailing_comma(self):
        """コメント + 末尾カンマの組み合わせ."""
        text = '{\n  "a": 1, // first\n  "b": 2, // second\n}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}

    def test_comment_in_string_preserved(self):
        """文字列内のコメント記号は保持される."""
        text = '{"url": "http://example.com/path"}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["url"] == "http://example.com/path"

    def test_raw_newlines_in_string(self):
        """文字列値内の生改行がエスケープされる."""
        text = '{"description": "line1\nline2\nline3", "confidence": 0.9}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["description"] == "line1\nline2\nline3"
        assert parsed["confidence"] == 0.9

    def test_raw_newlines_multiline_rca(self):
        """RCAレポートのような複数行テキストを含むJSON."""
        text = (
            "{\n"
            '  "root_causes": [{\n'
            '    "description": "CPU使用率が高い。\n'
            "詳細:\n"
            "- node-1: 95%\n"
            '- node-2: 88%",\n'
            '    "confidence": 0.85\n'
            "  }],\n"
            '  "metrics_summary": "概要:\n'
            'rate(cpu[5m]) max=95%"\n'
            "}"
        )
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["root_causes"][0]["confidence"] == 0.85
        assert "node-1" in parsed["root_causes"][0]["description"]

    def test_escaped_newlines_preserved(self):
        """既にエスケープ済みの \\n はそのまま保持される."""
        text = '{"msg": "line1\\nline2"}'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["msg"] == "line1\nline2"


class TestStripJsonComments:
    def test_line_comment(self):
        text = '{"key": "value"} // comment'
        result = strip_json_comments(text)
        assert json.loads(result.strip()) == {"key": "value"}

    def test_multiline_comments(self):
        text = '{\n  "a": 1, // comment 1\n  "b": 2 // comment 2\n}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}

    def test_block_comment(self):
        text = '{"key": /* block */ "value"}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_comment_in_string_preserved(self):
        text = '{"url": "http://example.com"}'
        result = strip_json_comments(text)
        assert json.loads(result) == {"url": "http://example.com"}

    def test_no_comments(self):
        text = '{"key": "value"}'
        result = strip_json_comments(text)
        assert result == text

    def test_investigation_plan_with_comments(self):
        """調査計画の典型的な LLM 出力（コメント付き）."""
        text = """{
  "promql_queries": [
    "up{namespace=\\"monitoring\\"}", // ノードの稼働状態
    "rate(container_cpu_usage_seconds_total{namespace=\\"monitoring\\"}[5m])" // CPU使用率
  ],
  "logql_queries": [
    "{namespace=\\"monitoring\\"} |= \\"error\\"" // エラーログ
  ],
  "target_instances": [],
  "time_range": {
    "start": "2026-03-09T00:00:00Z",
    "end": "2026-03-09T12:00:00Z"
  }
}"""
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert len(parsed["promql_queries"]) == 2
        assert len(parsed["logql_queries"]) == 1
