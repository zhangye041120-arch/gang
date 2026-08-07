"""Feed approved lessons back into the RAG retrieval pipeline.

The bridge mirrors the KnowledgeBase.search() contract so it can be called
alongside CBT knowledge without changing the agent's inner loop.
"""

from dataclasses import dataclass
import hashlib
import re
from typing import Any


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for match in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", match):
            result.update(match)
            result.update(match[i : i + 2] for i in range(len(match) - 1))
        else:
            result.add(match)
    return result


@dataclass(frozen=True)
class LessonChunk:
    lesson_id: str
    error_pattern: str
    content: str
    content_hash: str
    version: str = "lesson-v1"


class LessonBridge:
    """Query-approved lessons, optionally boosted by embedding similarity.

    This is deliberately separate from the CBT KnowledgeBase so that:
    - Lessons are always tagged as such (来源: 运营教训)
    - They don't pollute CBT knowledge versioning
    - Operators can review and revoke lessons independently
    """

    SOURCE = "operations-lesson"

    def __init__(self, quality_repository, embed_client: Any = None, *, top_k: int = 2):
        self._repo = quality_repository
        self._embed = embed_client
        self.top_k = top_k

    def search(self, query: str) -> list[tuple[LessonChunk, float]]:
        """Return approved lessons ranked by relevance to *query*."""
        lessons = self._repo.approved_lessons()
        if not lessons:
            return []

        chunks = [
            LessonChunk(
                lesson_id=record.lesson_id,
                error_pattern=record.error_pattern,
                content=record.content,
                content_hash=record.content_hash,
            )
            for record in lessons
        ]

        if self._embed is not None and len(chunks) > 1:
            return self._vector_rank(query, chunks)
        # Fallback: keyword overlap
        return self._lexical_rank(query, chunks)

    def context(self, query: str, *, top_k: int | None = None) -> tuple[str, list[dict[str, object]]]:
        results = self.search(query)[: (top_k or self.top_k)]
        sources: list[dict[str, object]] = []
        blocks: list[str] = []
        for chunk, score in results:
            sources.append({
                "source": self.SOURCE,
                "lesson_id": chunk.lesson_id,
                "error_pattern": chunk.error_pattern,
                "score": score,
            })
            blocks.append(f"[运营教训 {chunk.lesson_id} | 类型:{chunk.error_pattern}]\n{chunk.content}")
        return "\n\n".join(blocks), sources

    def _lexical_rank(self, query: str, chunks: list[LessonChunk]) -> list[tuple[LessonChunk, float]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored = []
        for chunk in chunks:
            content_tokens = _tokens(chunk.content)
            overlap = len(query_tokens & content_tokens)
            if not overlap:
                continue
            score = overlap / max(1, len(query_tokens))
            # Boost exact error_pattern match
            if chunk.error_pattern != "unknown" and chunk.error_pattern in query_tokens:
                score += 0.25
            scored.append((chunk, min(score, 1.0)))
        scored.sort(key=lambda item: -item[1])
        return scored[: self.top_k]

    def _vector_rank(self, query: str, chunks: list[LessonChunk]) -> list[tuple[LessonChunk, float]]:
        try:
            vector = self._embed.embed([query])[0]
            texts = [chunk.content for chunk in chunks]
            doc_vectors = self._embed.embed(texts)
            scored = []
            for chunk, doc_vec in zip(chunks, doc_vectors):
                similarity = self._cosine(vector, doc_vec)
                if similarity >= 0.3:  # minimum relevance threshold
                    scored.append((chunk, round(similarity, 4)))
            scored.sort(key=lambda item: -item[1])
            return scored[: self.top_k]
        except Exception:
            return self._lexical_rank(query, chunks)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
