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
