"""JSON 抽出・修復ユーティリティ.

LLM 出力からJSONを抽出し、不完全なJSON（閉じ忘れ、末尾カンマ等）を修復する。
"""

import re


def strip_json_comments(text: str) -> str:
    """JSON文字列から行コメント (//) とブロックコメント (/* */) を除去.

    文字列リテラル内のコメント記号はそのまま保持する。
    """
    result: list[str] = []
    i = 0
    in_string = False
    length = len(text)

    while i < length:
        ch = text[i]

        # 文字列リテラル内のエスケープ
        if in_string:
            if ch == "\\" and i + 1 < length:
                result.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
            result.append(ch)
            i += 1
            continue

        # 文字列リテラル開始
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue

        # 行コメント //
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            # 行末までスキップ
            end = text.find("\n", i)
            if end == -1:
                break  # 残り全てコメント
            i = end  # \n は保持
            continue

        # ブロックコメント /* */
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break  # 閉じられていないブロックコメント
            i = end + 2
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def _find_closing_fence(text: str, start: int) -> int | None:
    """コードフェンス ``` の正しい閉じ位置を探す.

    JSON値の中にエスケープされた ``` が含まれる場合（例: summaryフィールド内の
    ```promql ... ``` コードブロック）を考慮し、行頭の ``` のみを閉じフェンスとみなす。
    JSON文字列リテラル内の ``` は無視する。
    """
    pos = start
    while pos < len(text):
        idx = text.find("```", pos)
        if idx == -1:
            return None

        # JSON文字列リテラル内かどうかを判定:
        # start から idx までの間で未閉じの " があれば文字列内
        in_string = False
        for i in range(start, idx):
            ch = text[i]
            if ch == "\\" and in_string:
                continue
            if i > 0 and text[i - 1] == "\\":
                continue
            if ch == '"':
                in_string = not in_string

        if in_string:
            # 文字列リテラル内の ``` → スキップ
            pos = idx + 3
            continue

        # 行頭の ``` を閉じフェンスとみなす
        line_start = text.rfind("\n", start, idx)
        prefix = text[line_start + 1 : idx] if line_start >= 0 else text[start:idx] if idx == start else text[:idx]
        # 行頭（先頭空白のみ）の場合のみ閉じフェンス
        if prefix.strip() == "":
            return idx

        pos = idx + 3

    return None


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
        # JSON内に埋め込まれた ``` (PromQL等のコードブロック) を考慮し、
        # 有効なJSONとしてパース可能な最長の範囲を探す
        end = _find_closing_fence(text, start)
        if end is not None:
            return text[start:end].strip()
        # 閉じ ``` がない（出力切り詰め）→ 残り全体を返す
        return text[start:].strip()

    # ``` ... ``` 形式（言語指定なし）
    if "```" in text:
        start = text.index("```") + 3
        # 改行をスキップ
        while start < len(text) and text[start] in "\n\r":
            start += 1
        end = _find_closing_fence(text, start)
        if end is not None:
            candidate = text[start:end].strip()
        else:
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


def _escape_newlines_in_strings(text: str) -> str:
    """JSON文字列値内の生の改行を \\n にエスケープする.

    LLMが複数行テキストをJSON文字列値に直接埋め込む場合があり、
    これは JSON 仕様違反のため json.loads が失敗する。
    """
    result: list[str] = []
    i = 0
    in_string = False
    length = len(text)

    while i < length:
        ch = text[i]

        if in_string:
            if ch == "\\" and i + 1 < length:
                # エスケープシーケンスはそのまま保持
                result.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
                result.append(ch)
                i += 1
                continue
            if ch == "\n":
                result.append("\\n")
                i += 1
                continue
            if ch == "\r":
                result.append("\\r")
                i += 1
                continue
            if ch == "\t":
                result.append("\\t")
                i += 1
                continue
            result.append(ch)
            i += 1
            continue

        if ch == '"':
            in_string = True
        result.append(ch)
        i += 1

    return "".join(result)


def repair_truncated_json(text: str) -> str:
    """LLM出力が途中で切れた不完全なJSONを修復する.

    未閉じの文字列・配列・オブジェクトを閉じて有効なJSONにする。
    コメント除去、末尾カンマ除去、文字列値内の生改行エスケープも行う。
    """
    # まずコメントを除去
    text = strip_json_comments(text)
    # 文字列値内の生改行をエスケープ
    text = _escape_newlines_in_strings(text)

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
