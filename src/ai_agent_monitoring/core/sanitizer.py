"""LLMプロンプトインジェクション対策 — ユーザ入力のサニタイズ."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# プロンプトインジェクションの疑いがあるパターン
_PREV_PATTERN = r"(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)"
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"ignore\s+{_PREV_PATTERN}", re.IGNORECASE), "ignore previous instructions"),
    (re.compile(rf"disregard\s+{_PREV_PATTERN}", re.IGNORECASE), "disregard previous instructions"),
    (re.compile(rf"forget\s+{_PREV_PATTERN}", re.IGNORECASE), "forget previous instructions"),
    (re.compile(r"you\s+are\s+now\s+(a|an)\b", re.IGNORECASE), "role reassignment"),
    (re.compile(r"act\s+as\s+(a|an|if)\b", re.IGNORECASE), "role reassignment"),
    (re.compile(r"pretend\s+(you\s+are|to\s+be)\b", re.IGNORECASE), "role reassignment"),
    (re.compile(r"new\s+instructions?\s*:", re.IGNORECASE), "new instructions injection"),
    (re.compile(r"system\s*:\s*", re.IGNORECASE), "system prompt injection"),
    (re.compile(r"\[INST\]", re.IGNORECASE), "instruction tag injection"),
    (re.compile(r"<\|?(system|assistant|user)\|?>", re.IGNORECASE), "chat role tag injection"),
    (re.compile(r"```\s*(system|instruction)", re.IGNORECASE), "code block instruction injection"),
    (re.compile(r"override\s+(your|the)\s+(instructions?|rules?|behavior)", re.IGNORECASE), "instruction override"),
    (re.compile(r"do\s+not\s+follow\s+(your|the)\s+(instructions?|rules?)", re.IGNORECASE), "instruction override"),
]

# Markdownインジェクション用の特殊文字エスケープマッピング
_MARKDOWN_ESCAPE_CHARS = {
    "<!--": "&lt;!--",
    "-->": "--&gt;",
    "<script": "&lt;script",
    "</script": "&lt;/script",
}

# ユーザ入力デリミタ
_USER_INPUT_DELIMITER_START = "```user_input"
_USER_INPUT_DELIMITER_END = "```"


def detect_injection_patterns(text: str) -> list[str]:
    """テキスト内のプロンプトインジェクションパターンを検出.

    Args:
        text: 検査対象のテキスト

    Returns:
        検出されたパターンの説明リスト（検出なしの場合は空リスト）
    """
    detected: list[str] = []
    for pattern, description in _INJECTION_PATTERNS:
        if pattern.search(text):
            detected.append(description)
    return detected


def escape_markdown_injection(text: str) -> str:
    """Markdownインジェクションに使われる特殊文字をエスケープ.

    HTMLコメントやscriptタグなど、Markdownレンダリング時に
    悪用される可能性のある文字列をエスケープする。

    Args:
        text: エスケープ対象のテキスト

    Returns:
        エスケープ済みテキスト
    """
    result = text
    for original, escaped in _MARKDOWN_ESCAPE_CHARS.items():
        result = result.replace(original, escaped)
    return result


def wrap_with_delimiter(text: str) -> str:
    """ユーザ入力をデリミタで囲む.

    LLMがシステム指示とユーザ入力を区別しやすくするため、
    明確なデリミタで入力を囲む。

    Args:
        text: ユーザ入力テキスト

    Returns:
        デリミタで囲まれたテキスト
    """
    return f"{_USER_INPUT_DELIMITER_START}\n{text}\n{_USER_INPUT_DELIMITER_END}"


def sanitize_user_input(text: str) -> str:
    """ユーザ入力をサニタイズしてプロンプトに安全に挿入できるようにする.

    以下の処理を行う:
    1. プロンプトインジェクションパターンの検出とログ警告
    2. Markdownインジェクション用特殊文字のエスケープ
    3. デリミタによるユーザ入力の明確な区別

    検出時はリクエストを拒否せず、サニタイズした入力を使用する。

    Args:
        text: ユーザからの生入力テキスト

    Returns:
        サニタイズ済みテキスト（デリミタ付き）
    """
    # 1. インジェクションパターンの検出
    detected = detect_injection_patterns(text)
    if detected:
        logger.warning(
            "Potential prompt injection detected in user input: %s",
            detected,
        )

    # 2. Markdownインジェクション対策
    sanitized = escape_markdown_injection(text)

    # 3. デリミタで囲む
    return wrap_with_delimiter(sanitized)
