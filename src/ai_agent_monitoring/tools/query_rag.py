"""PromQL/LogQL ドキュメントのローカルRAG.

オフライン環境で動作するローカルドキュメント検索を提供。
BM25とTF-IDFを使用したキーワードベースの検索を実装。
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


@dataclass
class Document:
    """ドキュメントチャンク."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_id: str = ""

    def __post_init__(self) -> None:
        if not self.doc_id:
            self.doc_id = hashlib.md5(self.content.encode(), usedforsecurity=False).hexdigest()[:12]


@dataclass
class SearchResult:
    """検索結果."""

    document: Document
    score: float
    highlights: list[str] = field(default_factory=list)


class SimpleTokenizer:
    """シンプルなトークナイザー."""

    # 日本語と英語の両方に対応
    STOP_WORDS: ClassVar[set[str]] = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "and",
        "but",
        "if",
        "or",
        "because",
        "until",
        "while",
        "this",
        "that",
        "these",
        "those",
        "it",
        # 日本語
        "の",
        "は",
        "が",
        "を",
        "に",
        "で",
        "と",
        "も",
        "や",
        "など",
        "です",
        "ます",
        "する",
        "した",
        "して",
        "される",
        "された",
    }

    @classmethod
    def tokenize(cls, text: str) -> list[str]:
        """テキストをトークンに分割."""
        # 小文字化
        text = text.lower()
        # 特殊文字を空白に置換（ただしPromQL/LogQL記号は保持）
        text = re.sub(r"[^\w\s{}\[\]|=~!<>\"']", " ", text)
        # 空白で分割
        tokens = text.split()
        # ストップワードを除去
        tokens = [t for t in tokens if t not in cls.STOP_WORDS and len(t) > 1]
        return tokens


class BM25Index:
    """BM25インデックス."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents: list[Document] = []
        self.doc_lengths: list[int] = []
        self.avg_doc_length: float = 0.0
        self.doc_freqs: dict[str, int] = {}  # term -> ドキュメント出現数
        self.term_freqs: list[dict[str, int]] = []  # doc_idx -> {term: freq}
        self.N: int = 0

    def add_documents(self, documents: list[Document]) -> None:
        """ドキュメントをインデックスに追加."""
        for doc in documents:
            self.documents.append(doc)
            tokens = SimpleTokenizer.tokenize(doc.content)
            self.doc_lengths.append(len(tokens))

            # 単語頻度を計算
            term_freq: dict[str, int] = {}
            for token in tokens:
                term_freq[token] = term_freq.get(token, 0) + 1
            self.term_freqs.append(term_freq)

            # ドキュメント頻度を更新
            for term in set(tokens):
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        self.N = len(self.documents)
        self.avg_doc_length = sum(self.doc_lengths) / self.N if self.N > 0 else 0

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """クエリでドキュメントを検索."""
        query_tokens = SimpleTokenizer.tokenize(query)

        if not query_tokens:
            return []

        scores: list[tuple[int, float]] = []

        for doc_idx in range(self.N):
            score = 0.0
            doc_len = self.doc_lengths[doc_idx]
            term_freq = self.term_freqs[doc_idx]

            for term in query_tokens:
                if term not in term_freq:
                    continue

                tf = term_freq[term]
                df = self.doc_freqs.get(term, 0)

                # IDF計算
                idf = (self.N - df + 0.5) / (df + 0.5) if df > 0 else 0
                if idf > 0:
                    import math

                    idf = math.log(1 + idf)

                # BM25スコア計算
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avg_doc_length)
                score += idf * numerator / denominator

            if score > 0:
                scores.append((doc_idx, score))

        # スコアでソート
        scores.sort(key=lambda x: x[1], reverse=True)

        # 上位k件を返す
        results = []
        for doc_idx, score in scores[:top_k]:
            # ハイライト抽出
            highlights = self._extract_highlights(self.documents[doc_idx].content, query_tokens)
            results.append(
                SearchResult(
                    document=self.documents[doc_idx],
                    score=score,
                    highlights=highlights,
                )
            )

        return results

    def _extract_highlights(self, content: str, query_tokens: list[str], context_chars: int = 100) -> list[str]:
        """クエリトークンを含む部分を抽出."""
        highlights: list[str] = []
        content_lower = content.lower()

        for token in query_tokens:
            pos = content_lower.find(token)
            while pos != -1 and len(highlights) < 3:
                start = max(0, pos - context_chars)
                end = min(len(content), pos + len(token) + context_chars)
                highlight = content[start:end]
                if start > 0:
                    highlight = "..." + highlight
                if end < len(content):
                    highlight = highlight + "..."
                if highlight not in highlights:
                    highlights.append(highlight)
                pos = content_lower.find(token, pos + 1)

        return highlights


class QueryDocumentRAG:
    """PromQL/LogQLドキュメントのRAGリトリーバー."""

    DEFAULT_DOCS_PATH = Path(__file__).parent.parent.parent.parent / "docs" / "query_reference"

    def __init__(self, docs_path: Path | None = None) -> None:
        self.docs_path = docs_path or self.DEFAULT_DOCS_PATH
        self.index = BM25Index()
        self._initialized = False

    def initialize(self) -> None:
        """ドキュメントを読み込んでインデックスを構築."""
        if self._initialized:
            return

        documents = self._load_documents()
        if documents:
            self.index.add_documents(documents)
            logger.info(
                "Loaded %d document chunks from %s",
                len(documents),
                self.docs_path,
            )
        else:
            logger.warning("No documents found in %s", self.docs_path)

        self._initialized = True

    def _load_documents(self) -> list[Document]:
        """Markdownドキュメントを読み込んでチャンクに分割."""
        documents: list[Document] = []

        if not self.docs_path.exists():
            logger.warning("Documentation path does not exist: %s", self.docs_path)
            return documents

        for md_file in self.docs_path.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                chunks = self._split_markdown(content, md_file.name)
                documents.extend(chunks)
            except Exception as e:
                logger.error("Failed to load %s: %s", md_file, e)

        return documents

    def _split_markdown(self, content: str, filename: str) -> list[Document]:
        """Markdownをセクション単位でチャンクに分割."""
        chunks = []

        # ##で始まるセクションで分割
        sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)

        for section in sections:
            section = section.strip()
            if not section:
                continue

            # セクションタイトルを抽出
            title_match = re.match(r"^##\s+(.+)$", section, re.MULTILINE)
            title = title_match.group(1) if title_match else "Introduction"

            # サブセクションでさらに分割
            subsections = re.split(r"(?=^### )", section, flags=re.MULTILINE)

            for subsection in subsections:
                subsection = subsection.strip()
                if not subsection or len(subsection) < 50:
                    continue

                # サブセクションタイトルを抽出
                subtitle_match = re.match(r"^###\s+(.+)$", subsection, re.MULTILINE)
                subtitle = subtitle_match.group(1) if subtitle_match else ""

                # コードブロックを抽出してメタデータに追加
                code_blocks = re.findall(
                    r"```(?:promql|logql)?\n(.*?)```",
                    subsection,
                    re.DOTALL,
                )

                chunks.append(
                    Document(
                        content=subsection,
                        metadata={
                            "source": filename,
                            "title": title,
                            "subtitle": subtitle,
                            "code_examples": code_blocks,
                            "query_type": self._detect_query_type(filename),
                        },
                    )
                )

        return chunks

    def _detect_query_type(self, filename: str) -> str:
        """ファイル名からクエリタイプを検出."""
        if "promql" in filename.lower():
            return "promql"
        if "logql" in filename.lower():
            return "logql"
        return "unknown"

    def search(
        self,
        query: str,
        query_type: str | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """ドキュメントを検索.

        Args:
            query: 検索クエリ
            query_type: "promql" or "logql" でフィルタ
            top_k: 返す結果数

        Returns:
            SearchResult のリスト
        """
        if not self._initialized:
            self.initialize()

        # 検索実行
        results = self.index.search(query, top_k=top_k * 2)  # フィルタ用に多めに取得

        # query_typeでフィルタ
        if query_type:
            results = [r for r in results if r.document.metadata.get("query_type") == query_type]

        return results[:top_k]

    def get_relevant_context(
        self,
        user_query: str,
        query_type: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        """ユーザークエリに関連するコンテキストを取得.

        Args:
            user_query: ユーザーの質問/クエリ
            query_type: "promql" or "logql"
            max_tokens: 返すコンテキストの最大トークン数（概算）

        Returns:
            LLMに渡すコンテキスト文字列
        """
        results = self.search(user_query, query_type=query_type, top_k=5)

        if not results:
            return ""

        context_parts = []
        total_length = 0

        for result in results:
            doc = result.document
            section_text = f"### {doc.metadata.get('title', 'Reference')}"
            if doc.metadata.get("subtitle"):
                section_text += f" - {doc.metadata['subtitle']}"
            section_text += f"\n\n{doc.content}\n"

            # 大まかなトークン数（1トークン≒4文字と仮定）
            estimated_tokens = len(section_text) // 4
            if total_length + estimated_tokens > max_tokens:
                break

            context_parts.append(section_text)
            total_length += estimated_tokens

        return "\n---\n".join(context_parts)

    def get_examples_for_task(self, task_description: str) -> list[str]:
        """タスク説明から関連するクエリ例を取得.

        Args:
            task_description: 何を調査したいかの説明

        Returns:
            関連するクエリ例のリスト
        """
        results = self.search(task_description, top_k=5)

        examples = []
        for result in results:
            code_examples = result.document.metadata.get("code_examples", [])
            for example in code_examples:
                example = example.strip()
                if example and example not in examples:
                    examples.append(example)

        return examples[:10]  # 最大10件

    def save_index(self, path: Path) -> None:
        """インデックスをファイルに保存."""
        if not self._initialized:
            self.initialize()

        data = {
            "documents": [
                {
                    "content": doc.content,
                    "metadata": doc.metadata,
                    "doc_id": doc.doc_id,
                }
                for doc in self.index.documents
            ],
            "doc_lengths": self.index.doc_lengths,
            "doc_freqs": self.index.doc_freqs,
            "term_freqs": self.index.term_freqs,
        }

        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        logger.info("Index saved to %s", path)

    def load_index(self, path: Path) -> bool:
        """インデックスをファイルから読み込み."""
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text())

            self.index.documents = [
                Document(
                    content=d["content"],
                    metadata=d["metadata"],
                    doc_id=d["doc_id"],
                )
                for d in data["documents"]
            ]
            self.index.doc_lengths = data["doc_lengths"]
            self.index.doc_freqs = data["doc_freqs"]
            self.index.term_freqs = data["term_freqs"]
            self.index.N = len(self.index.documents)
            self.index.avg_doc_length = sum(self.index.doc_lengths) / self.index.N if self.index.N > 0 else 0
            self._initialized = True

            logger.info("Index loaded from %s", path)
            return True
        except Exception as e:
            logger.error("Failed to load index: %s", e)
            return False


# シングルトンインスタンス
_rag_instance: QueryDocumentRAG | None = None


def get_query_rag() -> QueryDocumentRAG:
    """RAGインスタンスを取得（シングルトン）."""
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = QueryDocumentRAG()
        _rag_instance.initialize()
    return _rag_instance
