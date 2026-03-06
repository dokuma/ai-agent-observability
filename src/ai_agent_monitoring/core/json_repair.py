"""JSON 抽出・修復ユーティリティ.

LLM 出力からJSONを抽出し、不完全なJSON（閉じ忘れ、末尾カンマ等）を修復する。
"""

import re


def extract_json(text: str) -> str:
    """テキストからJSON部分を抽出.

    ```json ... ``` 形式、``` ... ``` 形式、生の {...} を順に試行する。
    閉じ ``` がない場合（出力切り詰め）は残り全体を返し、修復に委ねる。

    Raises:
        ValueError: JSONが見つからない場合
    """
    # ```json ... ``` 形式を優先
    if "```json" in text:
        start = text.index("```json") + 7
        try:
            end = text.index("```", start)
            return text[start:end].strip()
        except ValueError:
            # 閉じ ``` がない（出力切り詰め）→ 残り全体を返す
            return text[start:].strip()

    # ``` ... ``` 形式（言語指定なし）
    if "```" in text:
        start = text.index("```") + 3
        # 改行をスキップ
        while start < len(text) and text[start] in "\n\r":
            start += 1
        try:
            end = text.index("```", start)
            candidate = text[start:end].strip()
        except ValueError:
            candidate = text[start:].strip()
        if candidate.startswith("{"):
            return candidate

    # 生の{...}を探す
    if "{" in text:
        start = text.index("{")
        if "}" in text:
            end = text.rindex("}") + 1
            return text[start:end]
        # 閉じブラケットがない（切り詰め）→ { 以降を返して修復に委ねる
        return text[start:]

    # JSONが見つからない
    raise ValueError(f"No JSON found in text (length={len(text)}): {text[:500]}")


def repair_truncated_json(text: str) -> str:
    """LLM出力が途中で切れた不完全なJSONを修復する.

    未閉じの文字列・配列・オブジェクトを閉じて有効なJSONにする。
    末尾カンマも除去する。
    """
    in_string = False
    escape_next = False
    stack: list[str] = []  # '{' or '['

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()

    suffix = ""
    if in_string:
        suffix += '"'
    for bracket in reversed(stack):
        suffix += "]" if bracket == "[" else "}"

    if not suffix:
        # 切り詰めはないが trailing comma がある場合に対応
        return re.sub(r",\s*([}\]])", r"\1", text)

    # 末尾の不完全なトークン（途切れた key/value）を除去してから閉じる
    trimmed = text.rstrip()
    # "key": で途切れた場合（値なしキー:）を検出して除去
    # コロンの前にキー名がある場合、カンマまで巻き戻す
    trimmed = re.sub(r',\s*"[^"]*"\s*:\s*$', "", trimmed)
    # 末尾が , : または空白の場合、最後の完全な要素まで戻す
    while trimmed and trimmed[-1] in (",", ":", " ", "\n", "\r", "\t"):
        trimmed = trimmed[:-1]
    # 未閉じ文字列を閉じた上で残りのブラケットを閉じる
    if in_string:
        trimmed += '"'
    for bracket in reversed(stack):
        trimmed += "]" if bracket == "[" else "}"
    # JSON本文中の trailing comma を除去
    trimmed = re.sub(r",\s*([}\]])", r"\1", trimmed)
    return trimmed
