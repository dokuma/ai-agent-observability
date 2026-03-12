"""RCAレポートの Markdown レンダラー."""

import re

from ai_agent_monitoring.core.models import RCAReport

# PromQL関数パターン（行頭で始まるクエリ）
_PROMQL_FUNC_PATTERN = re.compile(
    r"^(\s*)"
    r"((?:rate|sum|avg|max|min|count|count_values|topk|bottomk|quantile"
    r"|histogram_quantile|increase|delta|irate|idelta|absent|absent_over_time"
    r"|changes|resets|deriv|predict_linear|label_replace|label_join"
    r"|sort|sort_desc|time|vector|scalar|sgn|clamp|clamp_min|clamp_max"
    r"|round|ceil|floor|exp|ln|log2|log10|sqrt|abs"
    r"|up|node_|container_|kube_|process_|go_|http_|grpc_)"
    r"[^\n]{5,})",
    re.MULTILINE,
)

# LogQLセレクタパターン（行頭で始まる {label=...} 形式）
_LOGQL_SELECTOR_PATTERN = re.compile(
    r"^(\s*)(\{[^}]*(?:namespace|job|container|pod|instance|filename|app|service)"
    r'\s*[=~!]+\s*"[^"]*"[^}]*\}[^\n]*)',
    re.MULTILINE,
)


def _is_inside_code_block(text: str, match_start: int) -> bool:
    """マッチ位置がコードブロック（```）内にあるかチェック."""
    # マッチ位置より前の ``` の出現回数を数える
    before = text[:match_start]
    fence_count = before.count("```")
    # 奇数回なら開いたコードブロック内
    return fence_count % 2 == 1


def _is_inline_code(line: str, query: str) -> bool:
    """クエリがインラインコード（`...`）内にあるかチェック."""
    idx = line.find(query)
    if idx < 0:
        return False
    # クエリの前後に ` があるか
    before = line[:idx]
    after = line[idx + len(query) :]
    return before.rstrip().endswith("`") and after.lstrip().startswith("`")


def _format_summary_text(text: str) -> str:
    """summaryテキスト内のクエリを自動的にコードブロックで囲む後処理.

    既にコードブロックやインラインコード内にあるクエリはスキップする。
    """
    if not text:
        return text

    # 既にコードブロックを含んでいる場合はLLMがフォーマット済みとみなしスキップ
    if "```" in text:
        return text

    result = text

    # PromQLパターンの処理
    replacements: list[tuple[str, str]] = []
    for m in _PROMQL_FUNC_PATTERN.finditer(result):
        if _is_inside_code_block(result, m.start()):
            continue
        full_match = m.group(0)
        indent = m.group(1)
        query = m.group(2).strip()
        if _is_inline_code(full_match, query):
            continue
        old = full_match
        new = f"{indent}\n```promql\n{query}\n```\n"
        replacements.append((old, new))

    for old, new in replacements:
        result = result.replace(old, new, 1)

    # LogQLパターンの処理
    replacements = []
    for m in _LOGQL_SELECTOR_PATTERN.finditer(result):
        if _is_inside_code_block(result, m.start()):
            continue
        full_match = m.group(0)
        indent = m.group(1)
        query = m.group(2).strip()
        if _is_inline_code(full_match, query):
            continue
        old = full_match
        new = f"{indent}\n```logql\n{query}\n```\n"
        replacements.append((old, new))

    for old, new in replacements:
        result = result.replace(old, new, 1)

    return result


def render_rca_markdown(report: RCAReport) -> str:
    """RCAReportをMarkdown形式にレンダリング."""
    lines: list[str] = []

    # ヘッダー
    lines.append("# RCA レポート")
    lines.append("")
    lines.append(f"**生成日時:** {report.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # トリガー情報
    lines.append("## トリガー")
    lines.append("")
    if report.alert:
        lines.append("| 項目 | 内容 |")
        lines.append("|------|------|")
        lines.append("| **種別** | アラート |")
        lines.append(f"| **名前** | {report.alert.alert_name} |")
        lines.append(f"| **重要度** | {report.alert.severity} |")
        lines.append(f"| **インスタンス** | {report.alert.instance} |")
        lines.append(f"| **概要** | {report.alert.summary} |")
        lines.append(f"| **発生時刻** | {report.alert.starts_at.strftime('%Y-%m-%d %H:%M:%S')} |")
    elif report.user_query:
        lines.append("| 項目 | 内容 |")
        lines.append("|------|------|")
        lines.append("| **種別** | ユーザ問い合わせ |")
        lines.append(f"| **内容** | {report.user_query.raw_input} |")
    lines.append("")

    # 根本原因
    lines.append("## 根本原因分析")
    lines.append("")
    if report.root_causes:
        for i, rc in enumerate(report.root_causes, 1):
            confidence_bar = _confidence_bar(rc.confidence)
            lines.append(f"### 原因 {i}: {rc.description}")
            lines.append("")
            lines.append(f"**信頼度:** {rc.confidence:.0%} {confidence_bar}")
            lines.append("")
            if rc.evidence:
                lines.append("**エビデンス:**")
                lines.append("")
                for ev in rc.evidence:
                    lines.append(f"- {ev}")
                lines.append("")
    else:
        lines.append("根本原因を特定できませんでした。")
        lines.append("")

    # メトリクス分析
    lines.append("## メトリクス分析")
    lines.append("")
    if report.metrics_summary:
        lines.append(_format_summary_text(report.metrics_summary))
        lines.append("")

    # グラフ画像
    if report.panel_snapshots:
        for snap in report.panel_snapshots:
            if snap.query:
                lines.append(f"**クエリ:** `{snap.query}`")
                lines.append("")
            if snap.image_path:
                caption = snap.caption or f"Panel {snap.panel_id}"
                lines.append(f"![{caption}]({snap.image_path})")
                lines.append("")

    # ログ分析
    lines.append("## ログ分析")
    lines.append("")
    if report.logs_summary:
        lines.append(_format_summary_text(report.logs_summary))
        lines.append("")

    # ログ抜粋
    if report.log_excerpts:
        for excerpt in report.log_excerpts:
            if excerpt.caption:
                lines.append(f"### {excerpt.caption}")
                lines.append("")
            if excerpt.query:
                lines.append(f"**クエリ:** `{excerpt.query}`")
                lines.append("")
            if excerpt.entries:
                lines.append("```")
                for entry in excerpt.entries:
                    ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    lines.append(f"[{ts}] [{entry.level}] {entry.message}")
                lines.append("```")
                lines.append("")

    # Kubernetes分析
    if report.k8s_summary:
        lines.append("## Kubernetes分析")
        lines.append("")
        lines.append(_format_summary_text(report.k8s_summary))
        lines.append("")

    # 推奨アクション
    if report.recommendations:
        lines.append("## 推奨アクション")
        lines.append("")
        for rec in report.recommendations:
            lines.append(f"- {rec}")
        lines.append("")

    # フッター
    lines.append("---")
    lines.append("*Generated by AI Agent Monitoring System*")

    return "\n".join(lines)


def _confidence_bar(confidence: float) -> str:
    """信頼度をプログレスバーで表現."""
    filled = round(confidence * 10)
    return "█" * filled + "░" * (10 - filled)
