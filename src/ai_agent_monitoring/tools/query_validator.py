"""PromQL/LogQLクエリバリデーション.

LLMが生成したクエリの文法チェックと修正提案を行う。
"""

import re
from dataclasses import dataclass
from enum import Enum


class QueryType(Enum):
    """クエリの種類."""

    PROMQL = "promql"
    LOGQL = "logql"


@dataclass
class ValidationResult:
    """バリデーション結果."""

    is_valid: bool
    original_query: str
    corrected_query: str | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.warnings is None:
            self.warnings = []


class QueryValidator:
    """PromQL/LogQLクエリのバリデータ."""

    # SQLパターン（LogQL/PromQLでは使わない）
    SQL_PATTERNS = [
        (r"\bAND\b", "AND"),
        (r"\bOR\b", "OR"),
        (r"\bSELECT\b", "SELECT"),
        (r"\bFROM\b", "FROM"),
        (r"\bWHERE\b", "WHERE"),
        (r">=\s*['\"]", ">= with quotes (time comparison)"),
        (r"<=\s*['\"]", "<= with quotes (time comparison)"),
    ]

    # PromQLの有効なメトリクス名パターン
    PROMQL_METRIC_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*")

    # LogQLのラベルセレクタパターン
    LOGQL_LABEL_SELECTOR = re.compile(r"^\s*\{[^}]*\}")

    # PromQLの集約関数
    PROMQL_AGGREGATIONS = {
        "sum", "min", "max", "avg", "count", "stddev", "stdvar",
        "topk", "bottomk", "count_values", "quantile",
    }

    # PromQLのレンジ関数
    PROMQL_RANGE_FUNCTIONS = {
        "rate", "irate", "increase", "delta", "idelta",
        "deriv", "predict_linear", "changes", "resets",
        "avg_over_time", "min_over_time", "max_over_time",
        "sum_over_time", "count_over_time", "stddev_over_time",
        "stdvar_over_time", "last_over_time", "present_over_time",
        "quantile_over_time", "absent_over_time",
    }

    # LogQLのフィルタ演算子
    LOGQL_FILTER_OPS = {"|=", "!=", "|~", "!~"}

    def validate_promql(self, query: str) -> ValidationResult:
        """PromQLクエリを検証.

        Args:
            query: 検証するPromQLクエリ

        Returns:
            ValidationResult: バリデーション結果
        """
        errors: list[str] = []
        warnings: list[str] = []
        corrected = query.strip()

        # 空クエリチェック
        if not corrected:
            return ValidationResult(
                is_valid=False,
                original_query=query,
                errors=["クエリが空です"],
            )

        # SQLパターンの検出
        for pattern, name in self.SQL_PATTERNS:
            if re.search(pattern, corrected, re.IGNORECASE):
                errors.append(f"SQLの構文 '{name}' が検出されました。PromQLではありません。")

        # 基本構文チェック
        # メトリクス名またはアグリゲーション関数で始まるか
        first_token = corrected.split("(")[0].split("{")[0].strip()

        if first_token.lower() in self.PROMQL_AGGREGATIONS:
            # 集約関数の場合、括弧が必要
            if "(" not in corrected:
                errors.append(f"集約関数 '{first_token}' には括弧が必要です")
        elif first_token.lower() in self.PROMQL_RANGE_FUNCTIONS:
            # レンジ関数の場合、[duration]が必要
            if "[" not in corrected:
                warnings.append(f"レンジ関数 '{first_token}' には通常[duration]が必要です")
        elif not self.PROMQL_METRIC_PATTERN.match(first_token):
            errors.append(f"無効なメトリクス名: '{first_token}'")

        # ラベルセレクタの検証
        if "{" in corrected:
            label_match = re.search(r"\{([^}]*)\}", corrected)
            if label_match:
                label_content = label_match.group(1)
                self._validate_label_matchers(label_content, errors, warnings)

        # 括弧のバランスチェック
        if corrected.count("(") != corrected.count(")"):
            errors.append("括弧のバランスが取れていません")
        if corrected.count("{") != corrected.count("}"):
            errors.append("中括弧のバランスが取れていません")
        if corrected.count("[") != corrected.count("]"):
            errors.append("角括弧のバランスが取れていません")

        return ValidationResult(
            is_valid=len(errors) == 0,
            original_query=query,
            corrected_query=corrected if corrected != query else None,
            errors=errors if errors else None,
            warnings=warnings if warnings else None,
        )

    def validate_logql(self, query: str) -> ValidationResult:
        """LogQLクエリを検証.

        Args:
            query: 検証するLogQLクエリ

        Returns:
            ValidationResult: バリデーション結果
        """
        errors: list[str] = []
        warnings: list[str] = []
        corrected = query.strip()

        # 空クエリチェック
        if not corrected:
            return ValidationResult(
                is_valid=False,
                original_query=query,
                errors=["クエリが空です"],
            )

        # SQLパターンの検出（LogQLで最も多い間違い）
        for pattern, name in self.SQL_PATTERNS:
            if re.search(pattern, corrected, re.IGNORECASE):
                errors.append(
                    f"SQLの構文 '{name}' が検出されました。"
                    "LogQLは{{label=\"value\"}}形式を使用します。"
                )

        # LogQLは必ず{...}で始まる
        if not self.LOGQL_LABEL_SELECTOR.match(corrected):
            errors.append(
                "LogQLはラベルセレクタ {{...}} で始まる必要があります。"
                "例: {{job=\"varlogs\"}} |= \"error\""
            )
            # 自動修正を試みる
            corrected = self._attempt_logql_correction(corrected)
            if corrected != query.strip():
                warnings.append(f"自動修正を試みました: {corrected}")

        # ラベルセレクタ内の検証
        label_match = re.search(r"\{([^}]*)\}", corrected)
        if label_match:
            label_content = label_match.group(1)
            if not label_content.strip():
                errors.append("ラベルセレクタが空です。最低1つのラベルが必要です。")
            else:
                self._validate_label_matchers(label_content, errors, warnings)

        # 時間範囲がクエリ内に含まれていないかチェック
        time_patterns = [
            r"log_time\s*[<>=]",
            r"timestamp\s*[<>=]",
            r"@timestamp\s*[<>=]",
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
        ]
        for pattern in time_patterns:
            if re.search(pattern, corrected, re.IGNORECASE):
                errors.append(
                    "時間範囲はLogQLクエリ内ではなく、"
                    "APIパラメータ(start/end)で指定してください。"
                )
                break

        # 括弧のバランスチェック
        if corrected.count("{") != corrected.count("}"):
            errors.append("中括弧のバランスが取れていません")

        # パイプラインの検証
        if "|" in corrected:
            self._validate_logql_pipeline(corrected, errors, warnings)

        return ValidationResult(
            is_valid=len(errors) == 0,
            original_query=query,
            corrected_query=corrected if corrected != query.strip() else None,
            errors=errors if errors else None,
            warnings=warnings if warnings else None,
        )

    def _validate_label_matchers(
        self,
        label_content: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """ラベルマッチャーを検証."""
        # カンマで分割してラベルを検証
        matchers = [m.strip() for m in label_content.split(",") if m.strip()]

        for matcher in matchers:
            # 有効なマッチャー形式: label="value", label=~"regex", label!="value"
            valid_pattern = re.compile(
                r'^[a-zA-Z_][a-zA-Z0-9_]*\s*(!?=~?)\s*["\'][^"\']*["\']$'
            )
            if not valid_pattern.match(matcher):
                # シングルクォートの検出
                if "='" in matcher or "= '" in matcher:
                    warnings.append(
                        f"ラベル値にはダブルクォートを推奨: {matcher}"
                    )
                else:
                    errors.append(f"無効なラベルマッチャー: {matcher}")

    def _validate_logql_pipeline(
        self,
        query: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """LogQLパイプラインを検証."""
        # ラベルセレクタ以降を取得
        after_selector = re.sub(r"^\s*\{[^}]*\}", "", query).strip()

        if not after_selector:
            return

        # パイプで分割
        stages = after_selector.split("|")

        for i, stage in enumerate(stages):
            stage = stage.strip()
            if not stage:
                continue

            # 最初のステージはフィルタまたはパーサー
            if i == 0:
                # フィルタ: = "text", ~ "regex" など
                if stage.startswith(("=", "~", "!")):
                    # 有効なフィルタパターン
                    pass
                else:
                    warnings.append(f"不明なパイプラインステージ: |{stage}")

    def _attempt_logql_correction(self, query: str) -> str:
        """LogQLの自動修正を試みる.

        SQLライクなクエリをLogQL形式に変換を試みる。
        """
        corrected = query

        # label = 'value' を label="value" に変換
        corrected = re.sub(
            r"(\w+)\s*=\s*'([^']*)'",
            r'\1="\2"',
            corrected,
        )

        # AND を , に変換（ラベル間の場合）
        corrected = re.sub(r"\s+AND\s+", ", ", corrected, flags=re.IGNORECASE)

        # 時間条件を除去
        corrected = re.sub(
            r",?\s*\w*time\w*\s*[<>=]+\s*['\"][^'\"]*['\"]",
            "",
            corrected,
            flags=re.IGNORECASE,
        )

        # {}で囲む
        if not corrected.strip().startswith("{"):
            corrected = "{" + corrected.strip() + "}"

        # 空の{}を避ける
        if corrected.strip() == "{}":
            return query

        return corrected

    def validate(
        self, query: str, query_type: QueryType
    ) -> ValidationResult:
        """クエリを検証.

        Args:
            query: 検証するクエリ
            query_type: クエリの種類

        Returns:
            ValidationResult: バリデーション結果
        """
        if query_type == QueryType.PROMQL:
            return self.validate_promql(query)
        return self.validate_logql(query)

    def validate_and_fix(
        self, query: str, query_type: QueryType
    ) -> tuple[str, ValidationResult]:
        """クエリを検証し、可能なら修正.

        Args:
            query: 検証するクエリ
            query_type: クエリの種類

        Returns:
            tuple[str, ValidationResult]: 修正されたクエリとバリデーション結果
        """
        result = self.validate(query, query_type)

        if result.is_valid:
            return query, result

        # 修正されたクエリがある場合は再検証
        if result.corrected_query:
            corrected_result = self.validate(result.corrected_query, query_type)
            if corrected_result.is_valid:
                return result.corrected_query, corrected_result

        return query, result


# Few-shot例（LLMへのプロンプト用）
PROMQL_FEWSHOT_EXAMPLES = """
## PromQL クエリ例

### CPU使用率
- 全体: `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`
- 特定ジョブ: `rate(node_cpu_seconds_total{job="node-exporter", mode!="idle"}[5m])`

### メモリ使用率
- 使用中: `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
- 割合: `(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes) * 100`

### ディスク使用率
- 使用量: `node_filesystem_size_bytes - node_filesystem_avail_bytes`
- 割合: `(1 - node_filesystem_avail_bytes/node_filesystem_size_bytes) * 100`

### HTTPリクエスト
- リクエストレート: `rate(http_requests_total[5m])`
- エラーレート: `rate(http_requests_total{status=~"5.."}[5m])`
- レイテンシ: `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))`

### コンテナメトリクス
- CPU: `rate(container_cpu_usage_seconds_total{container!=""}[5m])`
- メモリ: `container_memory_usage_bytes{container!=""}`

### アラート関連
- アップ状態: `up{job="target-job"}`
- ターゲット数: `count(up)`
"""

LOGQL_FEWSHOT_EXAMPLES = """
## LogQL クエリ例

### 基本的なログ検索
- エラーログ: `{job="varlogs"} |= "error"`
- 警告以上: `{job="varlogs"} |~ "error|warn|fatal"`
- 特定ファイル: `{job="varlogs", filename="/var/log/syslog"}`

### Kubernetesログ
- 特定namespace: `{namespace="default"}`
- 特定Pod: `{namespace="monitoring", pod=~"prometheus.*"}`
- 特定コンテナ: `{namespace="default", container="app"} |= "error"`

### ログパイプライン
- JSONパース: `{job="app"} | json`
- 特定フィールド抽出: `{job="app"} | json | level="error"`
- 正規表現抽出: `{job="app"} | regexp "user=(?P<user>\\w+)"`

### ログ集約
- エラーカウント: `count_over_time({job="app"} |= "error" [5m])`
- ログレート: `rate({job="app"}[1m])`
- ラベル別集計: `sum by (level) (count_over_time({job="app"} | json [5m]))`

### 除外パターン
- 特定文字列除外: `{job="varlogs"} != "healthcheck"`
- 複数除外: `{job="varlogs"} != "healthcheck" != "metrics"`

### 重要: LogQLはSQLではありません
- NG: `SELECT * FROM logs WHERE level = 'error'`
- OK: `{job="varlogs"} |= "error"`
- NG: `pod_name = 'my-pod' AND timestamp >= '2024-01-01'`
- OK: `{pod="my-pod"}`（時間範囲はAPIパラメータで指定）
"""


def get_fewshot_examples(query_type: QueryType) -> str:
    """Few-shot例を取得."""
    if query_type == QueryType.PROMQL:
        return PROMQL_FEWSHOT_EXAMPLES
    return LOGQL_FEWSHOT_EXAMPLES


def get_all_fewshot_examples() -> str:
    """全てのFew-shot例を取得."""
    return PROMQL_FEWSHOT_EXAMPLES + "\n" + LOGQL_FEWSHOT_EXAMPLES
