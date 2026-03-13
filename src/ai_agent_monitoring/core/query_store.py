"""クエリ実行履歴の永続化とBM25検索."""

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_monitoring.core.models import QueryRecord
from ai_agent_monitoring.tools.query_rag import BM25Index, Document, SimpleTokenizer

logger = logging.getLogger(__name__)


class QueryStore:
    """SQLite + BM25 によるクエリ実行履歴ストア."""

    def __init__(self, db_path: str = "data/query_history.db") -> None:
        self._db_path = db_path
        self._index = BM25Index()
        self._tokenizer = SimpleTokenizer()

    def initialize(self) -> None:
        """テーブル作成と既存レコードのBM25インデックス構築."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS executed_queries (
                    id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    query_type TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'executed',
                    error_message TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT '',
                    result_stats_json TEXT NOT NULL DEFAULT '',
                    executed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            # 既存DBへのマイグレーション: result_stats_json カラム追加
            try:
                conn.execute("ALTER TABLE executed_queries ADD COLUMN result_stats_json TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # カラムが既に存在する場合
            conn.commit()

            rows = conn.execute(
                "SELECT id, query_type, tool_name, query_text, parameters_json "
                "FROM executed_queries ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

        if rows:
            docs = []
            for row in rows:
                text = self._build_searchable_text(row[1], row[2], row[3], row[4])
                docs.append(Document(content=text, doc_id=row[0]))
            self._index.add_documents(docs)
            logger.info("Loaded %d query records into BM25 index", len(docs))

    def save_query(self, investigation_id: str, record: QueryRecord) -> str:
        """1件のクエリ記録を保存."""
        query_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        params_json = json.dumps(record.parameters, ensure_ascii=False, default=str)

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO executed_queries
                (id, investigation_id, query_type, tool_name, query_text,
                 parameters_json, status, error_message, result_summary,
                 result_stats_json, executed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    query_id,
                    investigation_id,
                    record.query_type,
                    record.tool_name,
                    record.query_text,
                    params_json,
                    record.status,
                    record.error_message,
                    record.result_summary,
                    record.result_stats_json,
                    record.executed_at.isoformat(),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        searchable = self._build_searchable_text(record.query_type, record.tool_name, record.query_text, params_json)
        self._index.add_documents([Document(content=searchable, doc_id=query_id)])
        logger.debug("Saved query %s for investigation %s", query_id, investigation_id)
        return query_id

    def save_queries(self, investigation_id: str, records: list[QueryRecord]) -> int:
        """複数のクエリ記録をバッチ保存."""
        if not records:
            return 0
        count = 0
        for record in records:
            self.save_query(investigation_id, record)
            count += 1
        return count

    def get_by_investigation(self, investigation_id: str) -> list[dict[str, str]]:
        """調査IDで全クエリ記録を取得."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, query_type, tool_name, query_text, parameters_json, "
                "status, error_message, result_summary, result_stats_json, executed_at "
                "FROM executed_queries WHERE investigation_id = ? ORDER BY executed_at",
                (investigation_id,),
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": r[0],
                "query_type": r[1],
                "tool_name": r[2],
                "query_text": r[3],
                "parameters_json": r[4],
                "status": r[5],
                "error_message": r[6],
                "result_summary": r[7],
                "result_stats_json": r[8],
                "executed_at": r[9],
            }
            for r in rows
        ]

    def search(self, query: str, top_k: int = 10) -> list[dict[str, str]]:
        """BM25検索でクエリ履歴を検索."""
        results = self._index.search(query, top_k=top_k)
        if not results:
            return []

        ids = [sr.document.doc_id for sr in results]
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            sql = (
                "SELECT id, investigation_id, query_type, tool_name, query_text, "
                "parameters_json, status, error_message, result_summary, "
                "result_stats_json, executed_at "
                "FROM executed_queries WHERE id IN (" + placeholders + ")"
            )
            rows = conn.execute(sql, ids).fetchall()
        finally:
            conn.close()

        # 検索結果の順序を維持
        row_map = {r[0]: r for r in rows}
        output: list[dict[str, str]] = []
        for doc_id in ids:
            r = row_map.get(doc_id)
            if r:
                output.append(
                    {
                        "id": r[0],
                        "investigation_id": r[1],
                        "query_type": r[2],
                        "tool_name": r[3],
                        "query_text": r[4],
                        "parameters_json": r[5],
                        "status": r[6],
                        "error_message": r[7],
                        "result_summary": r[8],
                        "result_stats_json": r[9],
                        "executed_at": r[10],
                    }
                )
        return output

    def count(self) -> int:
        """総レコード数を返す."""
        conn = self._connect()
        try:
            result: int = conn.execute("SELECT COUNT(*) FROM executed_queries").fetchone()[0]
            return result
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    @staticmethod
    def _build_searchable_text(query_type: str, tool_name: str, query_text: str, parameters_json: str) -> str:
        """BM25インデックス用テキストを構築."""
        return f"{query_type} {tool_name} {query_text} {parameters_json}"
