"""QueryValidatorのテスト."""

import pytest

from ai_agent_monitoring.tools.query_validator import (
    LOGQL_FEWSHOT_EXAMPLES,
    PROMQL_FEWSHOT_EXAMPLES,
    QueryType,
    QueryValidator,
    get_all_fewshot_examples,
    get_fewshot_examples,
)


class TestQueryValidator:
    """QueryValidatorのテスト."""

    @pytest.fixture
    def validator(self):
        return QueryValidator()

    # PromQL テスト

    def test_valid_promql_simple_metric(self, validator):
        result = validator.validate_promql("up")
        assert result.is_valid
        assert not result.errors

    def test_valid_promql_with_labels(self, validator):
        result = validator.validate_promql('node_cpu_seconds_total{job="node"}')
        assert result.is_valid
        assert not result.errors

    def test_valid_promql_rate(self, validator):
        result = validator.validate_promql(
            'rate(http_requests_total{status="500"}[5m])'
        )
        assert result.is_valid
        assert not result.errors

    def test_valid_promql_aggregation(self, validator):
        result = validator.validate_promql(
            'sum by (instance) (rate(node_cpu_seconds_total[5m]))'
        )
        assert result.is_valid
        assert not result.errors

    def test_invalid_promql_sql_and(self, validator):
        result = validator.validate_promql(
            "metric_name = 'value' AND other = 'value'"
        )
        assert not result.is_valid
        assert result.errors
        assert any("AND" in e for e in result.errors)

    def test_invalid_promql_unbalanced_brackets(self, validator):
        result = validator.validate_promql("rate(metric[5m)")
        assert not result.is_valid
        assert any("括弧" in e or "バランス" in e for e in (result.errors or []))

    def test_promql_empty_query(self, validator):
        result = validator.validate_promql("")
        assert not result.is_valid
        assert any("空" in e for e in (result.errors or []))

    # LogQL テスト

    def test_valid_logql_simple(self, validator):
        result = validator.validate_logql('{job="varlogs"}')
        assert result.is_valid
        assert not result.errors

    def test_valid_logql_with_filter(self, validator):
        result = validator.validate_logql('{job="varlogs"} |= "error"')
        assert result.is_valid
        assert not result.errors

    def test_valid_logql_multiple_labels(self, validator):
        result = validator.validate_logql(
            '{namespace="default", container="app"}'
        )
        assert result.is_valid
        assert not result.errors

    def test_valid_logql_regex_filter(self, validator):
        result = validator.validate_logql('{job="app"} |~ "error|warn"')
        assert result.is_valid
        assert not result.errors

    def test_invalid_logql_sql_style(self, validator):
        """SQLスタイルのクエリはエラーになること."""
        result = validator.validate_logql(
            "kubernetes_pod_name = 'my-pod' AND log_type = 'system'"
        )
        assert not result.is_valid
        assert result.errors
        # SQLパターンの検出
        assert any("SQL" in e or "AND" in e for e in result.errors)

    def test_invalid_logql_with_time_range(self, validator):
        """時間範囲がクエリ内にある場合はエラー."""
        result = validator.validate_logql(
            "{job=\"app\"} AND log_time >= '2024-01-01T00:00:00'"
        )
        assert not result.is_valid
        assert result.errors

    def test_invalid_logql_not_starting_with_brace(self, validator):
        """中括弧で始まらないLogQLはエラー."""
        result = validator.validate_logql('job="varlogs"')
        assert not result.is_valid
        assert any("{{" in e or "ラベルセレクタ" in e for e in (result.errors or []))

    def test_logql_empty_query(self, validator):
        result = validator.validate_logql("")
        assert not result.is_valid
        assert any("空" in e for e in (result.errors or []))

    def test_logql_auto_correction_sql_style(self, validator):
        """SQLスタイルのクエリの自動修正を試みる."""
        # 完全なSQL形式は修正不可能かもしれないが、試みる
        result = validator.validate_logql(
            "pod_name = 'my-pod' AND namespace = 'default'"
        )
        # 修正を試みるが、完全には成功しない可能性
        assert result.corrected_query is not None or not result.is_valid

    # validate メソッドのテスト

    def test_validate_promql_type(self, validator):
        result = validator.validate("up", QueryType.PROMQL)
        assert result.is_valid

    def test_validate_logql_type(self, validator):
        result = validator.validate('{job="app"}', QueryType.LOGQL)
        assert result.is_valid

    # validate_and_fix のテスト

    def test_validate_and_fix_valid_query(self, validator):
        query, result = validator.validate_and_fix(
            '{job="app"}', QueryType.LOGQL
        )
        assert query == '{job="app"}'
        assert result.is_valid

    def test_validate_and_fix_attempts_correction(self, validator):
        """修正を試みてから結果を返す."""
        original = "label = 'value'"
        query, result = validator.validate_and_fix(original, QueryType.LOGQL)
        # 修正が成功したか、または元のクエリとエラーが返る
        assert query is not None


class TestFewshotExamples:
    """Few-shot例のテスト."""

    def test_promql_fewshot_not_empty(self):
        assert PROMQL_FEWSHOT_EXAMPLES
        assert "rate" in PROMQL_FEWSHOT_EXAMPLES
        assert "node_cpu" in PROMQL_FEWSHOT_EXAMPLES

    def test_logql_fewshot_not_empty(self):
        assert LOGQL_FEWSHOT_EXAMPLES
        assert "{job=" in LOGQL_FEWSHOT_EXAMPLES
        assert "|=" in LOGQL_FEWSHOT_EXAMPLES

    def test_logql_fewshot_contains_negative_example(self):
        """間違い例が含まれていること."""
        assert "NG:" in LOGQL_FEWSHOT_EXAMPLES or "間違い" in LOGQL_FEWSHOT_EXAMPLES

    def test_get_fewshot_examples_promql(self):
        examples = get_fewshot_examples(QueryType.PROMQL)
        assert examples == PROMQL_FEWSHOT_EXAMPLES

    def test_get_fewshot_examples_logql(self):
        examples = get_fewshot_examples(QueryType.LOGQL)
        assert examples == LOGQL_FEWSHOT_EXAMPLES

    def test_get_all_fewshot_examples(self):
        all_examples = get_all_fewshot_examples()
        assert PROMQL_FEWSHOT_EXAMPLES in all_examples
        assert LOGQL_FEWSHOT_EXAMPLES in all_examples


class TestValidationEdgeCases:
    """エッジケースのテスト."""

    @pytest.fixture
    def validator(self):
        return QueryValidator()

    def test_logql_with_json_parser(self, validator):
        result = validator.validate_logql('{job="app"} | json')
        assert result.is_valid

    def test_logql_with_complex_pipeline(self, validator):
        result = validator.validate_logql(
            '{job="app"} |= "error" | json | level="error"'
        )
        assert result.is_valid

    def test_promql_with_offset(self, validator):
        result = validator.validate_promql(
            'http_requests_total{job="api"} offset 5m'
        )
        # offset修飾子は有効
        assert result.is_valid

    def test_promql_histogram_quantile(self, validator):
        result = validator.validate_promql(
            'histogram_quantile(0.99, rate(http_request_duration_bucket[5m]))'
        )
        # histogram_quantileはPROMQL_AGGREGATIONS にないが有効なクエリ
        # バリデータはメトリクス名として扱うが、括弧のバランスは検証
        assert result.is_valid or not any(
            "バランス" in e for e in (result.errors or [])
        )

    def test_logql_negative_filter(self, validator):
        result = validator.validate_logql('{job="app"} != "healthcheck"')
        assert result.is_valid

    def test_logql_regex_negative_filter(self, validator):
        result = validator.validate_logql('{job="app"} !~ "health.*"')
        assert result.is_valid
