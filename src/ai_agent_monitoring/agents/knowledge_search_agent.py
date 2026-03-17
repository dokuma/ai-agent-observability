"""ナレッジ検索エージェント — RCAレポートと過去の観測データを統合検索."""

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ai_agent_monitoring.agents.prompts import KNOWLEDGE_SEARCH_SYSTEM_PROMPT
from ai_agent_monitoring.api.schemas import ReportSearchResponse, ReportSearchResult
from ai_agent_monitoring.core.models import StoredRCAReport
from ai_agent_monitoring.core.vector_store import VectorStore
from ai_agent_monitoring.tools.context_store import ContextStore

logger = logging.getLogger(__name__)

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe
except ImportError:

    def _observe(func: Any = None, **kwargs: Any) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func

        def decorator(f: Any) -> Any:
            return f

        return decorator


class KnowledgeSearchAgent:
    """過去のRCAレポートと観測データを検索し、LLMで回答を生成するエージェント."""

    def __init__(
        self,
        llm: Any,
        vector_store: VectorStore | None = None,
        use_context_store: bool = False,
    ) -> None:
        self._llm = llm
        self._vector_store = vector_store
        self._use_context_store = use_context_store

    @_observe(name="knowledge_search_translate_keywords", as_type="span")
    async def _translate_query_to_keywords(self, query: str) -> str:
        """ユーザクエリから英語検索キーワードを生成."""
        messages = [
            SystemMessage(
                content=(
                    "Convert this monitoring/infrastructure query to English search keywords. "
                    "Include: metric names, error types, service names, resource types, symptoms. "
                    "Output ONLY space-separated English keywords, no explanation."
                )
            ),
            HumanMessage(content=query),
        ]
        try:
            response = await self._llm.ainvoke(messages)
            result: str = response.content.strip()
            return result
        except Exception:
            logger.warning("Failed to translate query to English keywords")
            return ""

    async def _count_reports(self) -> int:
        """ベクトルストアのレポート数を取得."""
        if not self._vector_store:
            return 0
        try:
            return await self._vector_store.count()
        except Exception:
            return 0

    @_observe(name="knowledge_search_and_answer", as_type="span")
    async def search_and_answer(
        self, query: str, top_k: int = 5, observation_context: str = "", query_history: str = ""
    ) -> ReportSearchResponse:
        """クエリに基づいてRCAレポートと過去の観測データを検索し、LLMで回答を生成する."""
        total = await self._count_reports()
        logger.info("Knowledge search started: query=%s, total_reports=%d", query[:100], total)

        if not self._vector_store:
            return ReportSearchResponse(
                answer="ベクトルストアが初期化されていません。",
                results=[],
                total_reports=0,
            )

        en_query = await self._translate_query_to_keywords(query)
        combined_query = f"{en_query} {query}".strip() if en_query else query
        logger.info("Translated keywords: %s", en_query[:200] if en_query else "(empty)")

        vector_results = await self._vector_store.search(combined_query, top_k=top_k)
        logger.info("Search returned %d results", len(vector_results))

        if not vector_results and en_query:
            vector_results = await self._vector_store.search(query, top_k=top_k)

        if not vector_results:
            return ReportSearchResponse(
                answer="該当するRCAレポートが見つかりませんでした。",
                results=[],
                total_reports=total,
            )

        # Build context from search results
        context_parts: list[str] = []
        search_results: list[ReportSearchResult] = []

        # ContextStore: レポートのツール出力を全文インデックスし、
        # クエリに関連するチャンクのみをプロンプトに含める
        ctx_store = ContextStore(max_chunk_chars=800, search_limit=10) if self._use_context_store else None

        for i, vr in enumerate(vector_results, 1):
            stored = StoredRCAReport.from_qdrant_payload(vr.payload)
            if not stored:
                continue
            rca = stored.report
            root_causes_summary = "; ".join(rc.description for rc in rca.root_causes[:3]) if rca.root_causes else "不明"

            # agent_tool_outputs のコンテキスト
            tool_output_context = ""
            if rca.agent_tool_outputs:
                if ctx_store:
                    # ContextStore: 全文をインデックス（切り詰めなし）
                    for source, outputs in rca.agent_tool_outputs.items():
                        full_text = " ".join(outputs)
                        if full_text:
                            ctx_store.index_text(f"report_{i}_{source}", full_text)
                else:
                    # 従来方式: 各ソース先頭1500文字で切り詰め
                    tool_parts: list[str] = []
                    for source, outputs in rca.agent_tool_outputs.items():
                        combined = " ".join(outputs)[:1500]
                        if combined:
                            tool_parts.append(f"  {source}: {combined}")
                    if tool_parts:
                        tool_output_context = "\nツール実行結果:\n" + "\n".join(tool_parts) + "\n"

            # 環境スナップショット
            env_summary = stored.get_environment_summary()
            env_line = f"環境: {env_summary}\n" if env_summary else ""

            context_parts.append(
                f"--- レポート {i} (ID: {stored.id}, スコア: {vr.score:.2f}) ---\n"
                f"作成日時: {stored.created_at.isoformat()}\n"
                f"{env_line}"
                f"根本原因: {root_causes_summary}\n"
                f"メトリクス: {rca.metrics_summary or 'なし'}\n"
                f"ログ: {rca.logs_summary or 'なし'}\n"
                f"K8s: {rca.k8s_summary or 'なし'}\n"
                f"推奨事項: {'; '.join(rca.recommendations[:3]) if rca.recommendations else 'なし'}\n"
                f"{tool_output_context}"
            )

            # Determine trigger info
            alert_name = None
            trigger_type = "user_query"
            if rca.alert:
                alert_name = rca.alert.labels.get("alertname", rca.alert.alert_name)
                trigger_type = "alert"

            search_results.append(
                ReportSearchResult(
                    report_id=stored.id,
                    investigation_id=stored.investigation_id,
                    score=vr.score,
                    trigger_type=trigger_type,
                    alert_name=alert_name,
                    root_causes_summary=root_causes_summary,
                    created_at=stored.created_at,
                    highlights=[],
                )
            )

        if not search_results:
            if ctx_store:
                ctx_store.close()
            return ReportSearchResponse(
                answer="該当するRCAレポートが見つかりませんでした。",
                results=[],
                total_reports=total,
            )

        context = "\n".join(context_parts)

        # ContextStore: クエリに関連するツール出力チャンクを追加
        if ctx_store:
            relevant_chunks = ctx_store.search(combined_query, limit=15)
            if relevant_chunks:
                chunk_texts = [c["content"] for c in relevant_chunks]
                context += "\n\n## 関連するツール実行結果（BM25検索）\n" + "\n---\n".join(chunk_texts)
                logger.info(
                    "ContextStore: %d relevant chunks added from report tool outputs",
                    len(relevant_chunks),
                )
            ctx_store.close()

        # Generate answer with LLM
        logger.info("Generating LLM answer for knowledge search")
        human_content = f"## 検索クエリ\n{query}\n\n## 検索結果\n{context}"
        if observation_context:
            human_content += (
                "\n\n## 過去の類似観測データ\n"
                "以下は過去の調査で取得された類似の観測データです。"
                "回答の補足情報として活用してください:\n" + observation_context
            )
        if query_history:
            human_content += "\n\n## クエリ実行履歴\n以下は過去の調査で実行されたクエリの一覧です:\n" + query_history
        messages = [
            SystemMessage(content=KNOWLEDGE_SEARCH_SYSTEM_PROMPT),
            HumanMessage(content=human_content),
        ]

        response = await self._llm.ainvoke(messages)
        raw_content = response.content if hasattr(response, "content") else str(response)

        # LLMプロバイダによっては content がリスト形式のコンテンツブロックで返る場合がある
        if isinstance(raw_content, list):
            text_parts = []
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            answer = "\n".join(text_parts)
        else:
            answer = str(raw_content)

        # 空や JSON 構造だけの回答はフォールバック
        stripped = answer.strip()
        if not stripped or stripped in ("{}", "[]", "null"):
            logger.warning("Knowledge search returned empty/JSON-only answer: %r", stripped[:100])
            answer = (
                "過去のRCAレポートから情報が見つかりましたが、"
                "回答の生成に失敗しました。別のキーワードでお試しください。"
            )

        logger.info("Knowledge search completed: answer_length=%d", len(answer))

        return ReportSearchResponse(
            answer=answer,
            results=search_results,
            total_reports=total,
        )
