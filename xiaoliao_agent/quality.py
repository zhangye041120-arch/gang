from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from threading import Lock
from typing import Any
import uuid


@dataclass(frozen=True)
class InspectionLog:
    request_id: str
    message_id: str
    user_hash: str
    candidate_reply_ref: str
    crisis_detected: bool
    safety_violation: bool
    intent_accurate: bool
    age_appropriate: bool
    cbt_appropriate: bool
    issues: list[str]
    latency_ms: int
    main_model: str
    inspector_model: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost: float | None
    error_pattern: str
    lesson_ref: str | None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class LessonRecord:
    lesson_id: str
    content: str
    content_hash: str
    error_pattern: str
    prompt_version: str
    status: str = "pending"
    reviewer: str = ""
    reason: str = ""


@dataclass
class PromptPatch:
    patch_id: str
    error_pattern: str
    content: str
    status: str = "pending"
    reviewer: str = ""
    reason: str = ""
    test_report: str = ""
    target_version: str = ""


class MemoryQualityRepository:
    def __init__(self, *, fail_writes: bool = False):
        self.logs: dict[str, InspectionLog] = {}
        self.lessons: dict[str, LessonRecord] = {}
        self._lesson_by_hash: dict[str, str] = {}
        self.fail_writes = fail_writes
        self._lock = Lock()

    def write_log(self, item: InspectionLog) -> None:
        with self._lock:
            if self.fail_writes:
                raise RuntimeError("quality log unavailable")
            self.logs.setdefault(item.request_id, item)

    def add_lesson(self, content: str, error_pattern: str, prompt_version: str) -> LessonRecord:
        cleaned = content.strip()
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        with self._lock:
            if digest in self._lesson_by_hash:
                return self.lessons[self._lesson_by_hash[digest]]
            record = LessonRecord(uuid.uuid4().hex, cleaned, digest, error_pattern, prompt_version)
            self.lessons[record.lesson_id] = record
            self._lesson_by_hash[digest] = record.lesson_id
            return record

    def review_lesson(self, lesson_id: str, status: str, *, reviewer: str, reason: str) -> LessonRecord:
        if status not in {"approved", "rejected"} or not reviewer.strip() or not reason.strip():
            raise ValueError("lesson review requires status, reviewer, and reason")
        with self._lock:
            record = self.lessons[lesson_id]
            record.status = status
            record.reviewer = reviewer.strip()
            record.reason = reason.strip()
            return record

    def approved_lessons(self) -> list[LessonRecord]:
        with self._lock:
            return [record for record in self.lessons.values() if record.status == "approved"]


class QualityService:
    def __init__(self, repository):
        self.repository = repository
        self._retry: dict[str, InspectionLog] = {}
        self._lock = Lock()

    @property
    def retry_queue(self) -> list[InspectionLog]:
        with self._lock:
            return list(self._retry.values())

    def write_log(self, item: InspectionLog) -> list[str]:
        try:
            self.repository.write_log(item)
            with self._lock:
                self._retry.pop(item.request_id, None)
            return []
        except Exception:
            with self._lock:
                self._retry.setdefault(item.request_id, item)
            return ["inspection_log_write_failed"]

    def retry_failed(self) -> None:
        for item in self.retry_queue:
            self.write_log(item)


class PromptPatchStore:
    def __init__(self):
        self._items: dict[str, PromptPatch] = {}

    def create(self, error_pattern: str, content: str) -> PromptPatch:
        patch = PromptPatch(uuid.uuid4().hex, error_pattern, content.strip())
        self._items[patch.patch_id] = patch
        return patch

    def review(
        self,
        patch_id: str,
        status: str,
        *,
        reviewer: str,
        reason: str,
        test_report: str,
        target_version: str,
    ) -> PromptPatch:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid patch status")
        if not all(value.strip() for value in (reviewer, reason, test_report, target_version)):
            raise ValueError("patch review metadata is incomplete")
        patch = self._items[patch_id]
        patch.status = status
        patch.reviewer = reviewer.strip()
        patch.reason = reason.strip()
        patch.test_report = test_report.strip()
        patch.target_version = target_version.strip()
        return patch


class PostgresQualityRepository:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)

    def write_log(self, item: InspectionLog) -> None:
        import json

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_inspection_logs
                    (request_id, message_id, user_hash, candidate_reply_ref, crisis_detected,
                     safety_violation, intent_accurate, age_appropriate, cbt_appropriate, issues,
                     latency_ms, main_model, inspector_model, prompt_version, input_tokens,
                     output_tokens, total_tokens, cost, error_pattern, lesson_ref, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (item.request_id, item.message_id, item.user_hash, item.candidate_reply_ref,
                 item.crisis_detected, item.safety_violation, item.intent_accurate,
                 item.age_appropriate, item.cbt_appropriate, json.dumps(item.issues, ensure_ascii=False),
                 item.latency_ms, item.main_model, item.inspector_model, item.prompt_version,
                 item.input_tokens, item.output_tokens, item.total_tokens, item.cost,
                 item.error_pattern, item.lesson_ref, item.created_at),
            )

    def add_lesson(self, content: str, error_pattern: str, prompt_version: str) -> LessonRecord:
        digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO ai_lessons (lesson_id, content, content_hash, error_pattern, prompt_version, status, created_at)
                VALUES (%s,%s,%s,%s,%s,'pending',now())
                ON CONFLICT (content_hash) DO UPDATE SET content_hash=EXCLUDED.content_hash
                RETURNING lesson_id, content, content_hash, error_pattern, prompt_version, status, reviewer, reason
                """,
                (uuid.uuid4().hex, content.strip(), digest, error_pattern, prompt_version),
            ).fetchone()
        return LessonRecord(*row)

    def review_lesson(self, lesson_id: str, status: str, *, reviewer: str, reason: str) -> LessonRecord:
        if status not in {"approved", "rejected"} or not reviewer.strip() or not reason.strip():
            raise ValueError("lesson review metadata is incomplete")
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE ai_lessons SET status=%s, reviewer=%s, reason=%s, reviewed_at=now() WHERE lesson_id=%s RETURNING lesson_id, content, content_hash, error_pattern, prompt_version, status, reviewer, reason",
                (status, reviewer.strip(), reason.strip(), lesson_id),
            ).fetchone()
        return LessonRecord(*row)

    def approved_lessons(self) -> list[LessonRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT lesson_id, content, content_hash, error_pattern, prompt_version, status, reviewer, reason FROM ai_lessons WHERE status='approved'"
            ).fetchall()
        return [LessonRecord(*row) for row in rows]

    def create_patch(self, error_pattern: str, content: str) -> PromptPatch:
        patch = PromptPatch(uuid.uuid4().hex, error_pattern, content.strip())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ai_prompt_patches (patch_id, error_pattern, content, status, created_at) VALUES (%s,%s,%s,'pending',now())",
                (patch.patch_id, patch.error_pattern, patch.content),
            )
        return patch

    def review_patch(
        self,
        patch_id: str,
        status: str,
        *,
        reviewer: str,
        reason: str,
        test_report: str,
        target_version: str,
    ) -> PromptPatch:
        if status not in {"approved", "rejected"} or not all(
            value.strip() for value in (reviewer, reason, test_report, target_version)
        ):
            raise ValueError("patch review metadata is incomplete")
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE ai_prompt_patches SET status=%s, reviewer=%s, reason=%s, test_report=%s, target_version=%s, reviewed_at=now() WHERE patch_id=%s RETURNING patch_id, error_pattern, content, status, reviewer, reason, test_report, target_version",
                (status, reviewer.strip(), reason.strip(), test_report.strip(), target_version.strip(), patch_id),
            ).fetchone()
        return PromptPatch(*row)


from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
import uuid


ReviewStatus = Literal["pending", "approved", "rejected"]


@dataclass
class LessonCandidate:
    candidate_id: str
    content: str
    error_pattern: str
    prompt_version: str
    status: ReviewStatus
    created_at: str
    reviewer: str = ""
    reason: str = ""


class LessonRepository(Protocol):
    def add_candidate(self, content: str, error_pattern: str, prompt_version: str) -> LessonCandidate:
        ...

    def list_candidates(self, status: ReviewStatus | None = None) -> list[LessonCandidate]:
        ...

    def review(
        self,
        candidate_id: str,
        status: ReviewStatus,
        *,
        reviewer: str,
        reason: str,
    ) -> LessonCandidate:
        ...


class MemoryLessonRepository:
    """Temporary review queue. Step 8 will replace it with persistence."""

    def __init__(self):
        self._items: dict[str, LessonCandidate] = {}

    def add_candidate(self, content: str, error_pattern: str, prompt_version: str) -> LessonCandidate:
        candidate = LessonCandidate(
            candidate_id=uuid.uuid4().hex,
            content=content,
            error_pattern=error_pattern,
            prompt_version=prompt_version,
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._items[candidate.candidate_id] = candidate
        return candidate

    def get(self, candidate_id: str) -> LessonCandidate:
        try:
            return self._items[candidate_id]
        except KeyError as exc:
            raise KeyError("lesson candidate not found") from exc

    def list_candidates(self, status: ReviewStatus | None = None) -> list[LessonCandidate]:
        values = list(self._items.values())
        return [item for item in values if status is None or item.status == status]

    def review(
        self,
        candidate_id: str,
        status: ReviewStatus,
        *,
        reviewer: str,
        reason: str,
    ) -> LessonCandidate:
        if status not in {"approved", "rejected"}:
            raise ValueError("review status must be approved or rejected")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        candidate = self.get(candidate_id)
        candidate.status = status
        candidate.reviewer = reviewer.strip()
        candidate.reason = reason.strip()
        return candidate


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
