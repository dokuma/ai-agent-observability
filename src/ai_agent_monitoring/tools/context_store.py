"""Context Store — FTS5 ベースのツール出力インデックス.

MCP ツールの出力を SQLite FTS5 にインデックスし、
BM25 検索で関連性の高いチャンクのみを返却する。
盲目的な文字数打ち切り (_truncate_tool_result) の代替として、
ツール出力の情報品質を維持したまま圧縮する。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class ContextStore:
    """ツール出力を FTS5 にインデックスし、BM25 検索で取得するストア.

    調査セッションごとにインメモリ SQLite DB を使用し、
    セッション終了時に自動的に破棄される。
    """

    def __init__(
        self,
        max_chunk_chars: int = 500,
        search_limit: int = 5,
    ):
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self._max_chunk_chars = max_chunk_chars
        self._search_limit = search_limit
        self._init_fts5()

    def _init_fts5(self) -> None:
        """FTS5 仮想テーブルを作成."""
        self._db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                source_label,
                tool_name,
                chunk_index,
                content,
                tokenize='porter unicode61'
            );
        """)

    def index(
        self,
        tool_name: str,
        result: dict[str, Any],
    ) -> str:
        """ツール出力をチャンク分割して FTS5 に格納する.

        Args:
            tool_name: MCP ツール名
            result: _preprocess_result 後のツール結果

        Returns:
            source_label（このツール出力を識別する一意ラベル）
        """
        source_label = f"{tool_name}_{uuid.uuid4().hex[:8]}"
        text = self._extract_text(result)

        if not text:
            return source_label

        chunks = self._chunk_text(text)
        with self._db:
            for i, chunk in enumerate(chunks):
                self._db.execute(
                    "INSERT INTO chunks(source_label, tool_name, chunk_index, content) VALUES (?, ?, ?, ?)",
                    (source_label, tool_name, str(i), chunk),
                )

        logger.debug(
            "Indexed %d chunks for %s (total %d chars)",
            len(chunks),
            source_label,
            len(text),
        )
        return source_label

    def get_by_source(
        self,
        source_label: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """特定ツール出力のチャンクをインデックス順で取得.

        Args:
            source_label: index() が返した source_label
            limit: 取得件数（None の場合は search_limit を使用）

        Returns:
            チャンクのリスト
        """
        max_results = limit or self._search_limit
        rows = self._db.execute(
            "SELECT source_label, tool_name, chunk_index, content "
            "FROM chunks WHERE source_label = ? "
            "ORDER BY CAST(chunk_index AS INTEGER) LIMIT ?",
            (source_label, max_results),
        ).fetchall()
        return [dict(r) for r in rows]

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 ランキングで全文検索.

        Args:
            query: 検索クエリ
            limit: 取得件数

        Returns:
            BM25 スコア順のチャンクリスト
        """
        max_results = limit or self._search_limit
        # FTS5 の MATCH 構文に渡す前にクエリをサニタイズ
        sanitized = self._sanitize_query(query)
        if not sanitized:
            return []

        try:
            rows = self._db.execute(
                "SELECT source_label, tool_name, chunk_index, content, "
                "bm25(chunks) AS score "
                "FROM chunks WHERE chunks MATCH ? "
                "ORDER BY score LIMIT ?",
                (sanitized, max_results),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            logger.warning("FTS5 search failed for query: %s", query)
            return []

    def format_as_tool_result(
        self,
        chunks: list[dict[str, Any]],
        original_chars: int,
    ) -> dict[str, Any]:
        """チャンクリストを _truncate_tool_result 互換の形式に変換.

        Args:
            chunks: get_by_source() or search() の結果
            original_chars: インデックス前の元データサイズ

        Returns:
            ToolMessage に格納可能な dict
        """
        if not chunks:
            return {"content": [{"type": "text", "text": ""}]}

        texts = [c["content"] for c in chunks]
        combined = "\n---\n".join(texts)
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": combined}],
        }
        total_chunks = self._count_chunks_for_source(chunks[0].get("source_label", ""))
        shown = len(chunks)
        if shown < total_chunks:
            result["_context_mode"] = True
            result["_shown_chunks"] = shown
            result["_total_chunks"] = total_chunks
            result["_original_chars"] = original_chars
        return result

    def close(self) -> None:
        """DB 接続をクローズ."""
        self._db.close()

    # --- private ---

    def _extract_text(self, result: dict[str, Any]) -> str:
        """ツール結果からテキストコンテンツを抽出."""
        content = result.get("content", [])
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    texts.append(text)
        if texts:
            return "\n".join(texts)

        # error キーがある場合はそのテキストを返す
        if "error" in result:
            return str(result["error"])

        # content が空リストの場合は空文字を返す（インデックスしない）
        if isinstance(content, list) and len(content) == 0:
            return ""

        return json.dumps(result, ensure_ascii=False, default=str)

    def _chunk_text(self, text: str) -> list[str]:
        """テキストをチャンクに分割.

        戦略:
        1. 空行区切り（段落）でまず分割
        2. 各段落が max_chunk_chars を超える場合は行単位で再分割
        """
        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if current_len + len(para) + 2 <= self._max_chunk_chars:
                current.append(para)
                current_len += len(para) + 2
            else:
                if current:
                    chunks.append("\n\n".join(current))
                # 段落自体が大きい場合は行単位で分割
                if len(para) > self._max_chunk_chars:
                    chunks.extend(self._chunk_long_paragraph(para))
                    current = []
                    current_len = 0
                else:
                    current = [para]
                    current_len = len(para)

        if current:
            chunks.append("\n\n".join(current))

        return chunks if chunks else [text[: self._max_chunk_chars]]

    def _chunk_long_paragraph(self, para: str) -> list[str]:
        """長い段落を行単位でチャンクに分割."""
        lines = para.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            if current_len + len(line) + 1 <= self._max_chunk_chars:
                current.append(line)
                current_len += len(line) + 1
            else:
                if current:
                    chunks.append("\n".join(current))
                current = [line]
                current_len = len(line)

        if current:
            chunks.append("\n".join(current))
        return chunks

    def _sanitize_query(self, query: str) -> str:
        """FTS5 MATCH 構文用にクエリをサニタイズ.

        特殊文字を除去し、単語を OR 接続にする。
        """
        # FTS5 の特殊文字を除去
        cleaned = query.replace('"', " ").replace("'", " ")
        for ch in "(){}[]^*~:":
            cleaned = cleaned.replace(ch, " ")
        words = cleaned.split()
        if not words:
            return ""
        # 各単語を引用符で囲んで OR 接続
        return " OR ".join(f'"{w}"' for w in words if w)

    def _count_chunks_for_source(self, source_label: str) -> int:
        """指定 source_label のチャンク総数を返す."""
        if not source_label:
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE source_label = ?",
            (source_label,),
        ).fetchone()
        return row["cnt"] if row else 0
