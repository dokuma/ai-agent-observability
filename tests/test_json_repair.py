"""JSON 抽出・修復ユーティリティのテスト."""

import json

import pytest

from ai_agent_monitoring.core.json_repair import extract_json, repair_truncated_json


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
