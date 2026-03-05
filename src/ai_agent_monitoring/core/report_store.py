"""RCAレポートの永続化とBM25検索."""

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_monitoring.core.models import RCAReport, StoredRCAReport
from ai_agent_monitoring.tools.query_rag import BM25Index, Document, SimpleTokenizer

logger = logging.getLogger(__name__)


class ReportStore:
    """SQLite + BM25 によるRCAレポートストア."""

    def __init__(self, db_path: str = "data/rca_reports.db") -> None:
        self._db_path = db_path
        self._index = BM25Index()
        self._tokenizer = SimpleTokenizer()

    def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rca_reports (
                    id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    alert_name TEXT,
                    alert_summary TEXT,
                    user_query_raw TEXT,
                    root_causes_text TEXT,
                    metrics_summary TEXT,
                    logs_summary TEXT,
                    k8s_summary TEXT,
                    recommendations_text TEXT,
                    report_json TEXT NOT NULL,
                    markdown TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.commit()

            rows = conn.execute("SELECT id, report_json FROM rca_reports ORDER BY created_at").fetchall()
        finally:
            conn.close()

        if rows:
            docs = []
            for row in rows:
                report_id, report_json = row
                text = self._extract_searchable_text_from_json(report_json)
                if not text.strip():
                    logger.warning("Empty searchable text for report %s", report_id)
                docs.append(Document(content=text, doc_id=report_id))
            self._index.add_documents(docs)
            logger.info("Loaded %d reports into BM25 index", len(docs))

    def save_report(self, investigation_id: str, report: RCAReport) -> str:
        report_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        report_json = report.model_dump_json()

        alert_name = report.alert.alert_name if report.alert else None
        alert_summary = report.alert.summary if report.alert else None
        user_query_raw = report.user_query.raw_input if report.user_query else None
        root_causes_text = "\n".join(rc.description for rc in report.root_causes)
        recommendations_text = "\n".join(report.recommendations)

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO rca_reports
                (id, investigation_id, trigger_type, alert_name, alert_summary,
                 user_query_raw, root_causes_text, metrics_summary, logs_summary,
                 k8s_summary, recommendations_text, report_json, markdown, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id,
                    investigation_id,
                    report.trigger_type.value,
                    alert_name,
                    alert_summary,
                    user_query_raw,
                    root_causes_text,
                    report.metrics_summary,
                    report.logs_summary,
                    report.k8s_summary,
                    recommendations_text,
                    report_json,
                    report.markdown,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # agent_tool_outputs を結合（各ソースの先頭部分を含める）
        tool_outputs_parts: list[str] = []
        for outputs in report.agent_tool_outputs.values():
            tool_outputs_parts.extend(outputs)
        agent_tool_outputs_text = " ".join(tool_outputs_parts)

        searchable_text = self._build_searchable_text(
            alert_name=alert_name,
            alert_summary=alert_summary,
            user_query_raw=user_query_raw,
            root_causes_text=root_causes_text,
            metrics_summary=report.metrics_summary,
            logs_summary=report.logs_summary,
            k8s_summary=report.k8s_summary,
            recommendations_text=recommendations_text,
            agent_tool_outputs_text=agent_tool_outputs_text,
            search_keywords_en=report.search_keywords_en,
        )
        tokens = self._tokenizer.tokenize(searchable_text)
        logger.debug("Searchable text tokens: %d for report %s", len(tokens), report_id)
        self._index.add_documents([Document(content=searchable_text, doc_id=report_id)])
        logger.info("Saved report %s for investigation %s", report_id, investigation_id)
        return report_id

    def search(self, query: str, top_k: int = 5) -> list[tuple[StoredRCAReport, float, list[str]]]:
        tokens = self._tokenizer.tokenize(query)
        logger.debug("Search query tokens: %s", tokens)
        results = self._index.search(query, top_k=top_k)
        output: list[tuple[StoredRCAReport, float, list[str]]] = []
        for sr in results:
            stored = self.get_report(sr.document.doc_id)
            if stored:
                output.append((stored, sr.score, sr.highlights))
        return output

    def get_report(self, report_id: str) -> StoredRCAReport | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, investigation_id, report_json, created_at FROM rca_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return StoredRCAReport(
            id=row[0],
            investigation_id=row[1],
            report=RCAReport.model_validate_json(row[2]),
            created_at=datetime.fromisoformat(row[3]),
        )

    def list_reports(self, offset: int = 0, limit: int = 20) -> tuple[list[StoredRCAReport], int]:
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM rca_reports").fetchone()[0]
            rows = conn.execute(
                "SELECT id, investigation_id, report_json, created_at "
                "FROM rca_reports ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        finally:
            conn.close()

        reports = [
            StoredRCAReport(
                id=r[0],
                investigation_id=r[1],
                report=RCAReport.model_validate_json(r[2]),
                created_at=datetime.fromisoformat(r[3]),
            )
            for r in rows
        ]
        return reports, total

    def count(self) -> int:
        conn = self._connect()
        try:
            result: int = conn.execute("SELECT COUNT(*) FROM rca_reports").fetchone()[0]
            return result
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _build_searchable_text(
        self,
        *,
        alert_name: str | None,
        alert_summary: str | None,
        user_query_raw: str | None,
        root_causes_text: str,
        metrics_summary: str,
        logs_summary: str,
        k8s_summary: str,
        recommendations_text: str,
        agent_tool_outputs_text: str = "",
        search_keywords_en: str = "",
    ) -> str:
        parts = [
            p
            for p in [
                alert_name,
                alert_summary,
                user_query_raw,
                root_causes_text,
                metrics_summary,
                logs_summary,
                k8s_summary,
                recommendations_text,
                agent_tool_outputs_text,
                search_keywords_en,
            ]
            if p
        ]
        return " ".join(parts)

    def _extract_searchable_text_from_json(self, report_json: str) -> str:
        data = json.loads(report_json)
        alert = data.get("alert") or {}
        user_query = data.get("user_query") or {}
        root_causes = data.get("root_causes") or []
        agent_tool_outputs = data.get("agent_tool_outputs") or {}
        tool_outputs_parts: list[str] = []
        for outputs in agent_tool_outputs.values():
            if isinstance(outputs, list):
                tool_outputs_parts.extend(outputs)
        return self._build_searchable_text(
            alert_name=alert.get("alert_name"),
            alert_summary=alert.get("summary"),
            user_query_raw=user_query.get("raw_input"),
            root_causes_text="\n".join(rc.get("description", "") for rc in root_causes),
            metrics_summary=data.get("metrics_summary", ""),
            logs_summary=data.get("logs_summary", ""),
            k8s_summary=data.get("k8s_summary", ""),
            recommendations_text="\n".join(data.get("recommendations", [])),
            agent_tool_outputs_text=" ".join(tool_outputs_parts),
            search_keywords_en=data.get("search_keywords_en", ""),
        )
