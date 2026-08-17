from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
from threading import RLock
from typing import Any, Iterator

from .content_refs import HmacReferenceService


class SubjectDeletedError(PermissionError):
    pass


class SubjectPrivacyGuard:
    def __init__(self, reference_service: HmacReferenceService):
        self.reference_service = reference_service
        self._locks: dict[str, RLock] = {}
        self._statuses: dict[str, str] = {}
        self._lock = RLock()

    def subject_candidates(self, user_id: str) -> tuple[str, ...]:
        candidate_method = getattr(
            self.reference_service, "subject_hmac_candidates", None
        )
        if candidate_method is not None:
            return tuple(candidate_method(user_id))
        return (self.reference_service.subject_hmac(user_id),)

    def _user_lock(self, user_id: str) -> RLock:
        with self._lock:
            return self._locks.setdefault(user_id, RLock())

    def _subject_lock(self, subject_hmac: str) -> RLock:
        with self._lock:
            return self._locks.setdefault("subject:" + subject_hmac, RLock())

    @contextmanager
    def memory_write(self, user_id: str) -> Iterator[None]:
        with self._user_lock(user_id):
            if any(
                candidate in self._statuses
                for candidate in self.subject_candidates(user_id)
            ):
                raise SubjectDeletedError("subject is deleted")
            yield

    @contextmanager
    def memory_subject_write(self, subject_hmac: str) -> Iterator[None]:
        with self._subject_lock(subject_hmac):
            if subject_hmac in self._statuses:
                raise SubjectDeletedError("subject is deleted")
            yield

    @contextmanager
    def memory_deletion(self, user_id: str) -> Iterator[tuple[str, ...]]:
        with self._user_lock(user_id):
            candidates = self.subject_candidates(user_id)
            with ExitStack() as stack:
                for candidate in candidates:
                    stack.enter_context(self._subject_lock(candidate))
                    self._statuses[candidate] = "deleting"
                try:
                    yield candidates
                except Exception:
                    raise
                else:
                    for candidate in candidates:
                        self._statuses[candidate] = "deleted"

    def is_tombstoned(self, user_id: str) -> bool:
        with self._user_lock(user_id):
            return any(
                candidate in self._statuses
                for candidate in self.subject_candidates(user_id)
            )

    def protect_postgres_write(self, connection: Any, user_id: str) -> str:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"privacy-subject:{len(user_id)}:{user_id}",),
        )
        candidates = self.subject_candidates(user_id)
        row = connection.execute(
            "SELECT subject_hmac FROM ai_privacy_tombstones WHERE subject_hmac = ANY(%s) LIMIT 1",
            (list(candidates),),
        ).fetchone()
        if row:
            raise SubjectDeletedError("subject is deleted")
        return candidates[0]

    def protect_postgres_subject(
        self,
        connection: Any,
        subject_hmac: str,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("privacy-hmac:" + subject_hmac,),
        )
        if connection.execute(
            "SELECT 1 FROM ai_privacy_tombstones WHERE subject_hmac=%s",
            (subject_hmac,),
        ).fetchone():
            raise SubjectDeletedError("subject is deleted")


@dataclass(frozen=True)
class DeletionReport:
    status: str
    subject_hmac: str
    database_counts: dict[str, int]
    redis_cleaned: bool


@dataclass
class _DeletionAudit:
    request_id: str
    subject_hmac: str
    subject_candidates: tuple[str, ...]
    status: str
    database_counts: dict[str, int]


class PrivacyDeletionService:
    def __init__(
        self,
        *,
        reference_service: HmacReferenceService,
        privacy_guard: SubjectPrivacyGuard,
        user_data_service: Any,
        memory_service: Any,
        action_service: Any,
        reminder_service: Any,
        crisis_repository: Any,
        quality_service: Any,
        lesson_repository: Any | None = None,
        runtime_state: Any | None = None,
        database_url: str = "",
        legacy_quality_salt: str = "",
    ):
        self.reference_service = reference_service
        self.privacy_guard = privacy_guard
        self.user_data_service = user_data_service
        self.memory_service = memory_service
        self.action_service = action_service
        self.reminder_service = reminder_service
        self.crisis_repository = crisis_repository
        self.quality_service = quality_service
        self.lesson_repository = lesson_repository
        self.runtime_state = runtime_state
        self.database_url = database_url
        self.legacy_quality_salt = legacy_quality_salt
        self._audits: dict[str, _DeletionAudit] = {}
        self._subject_audits: dict[str, str] = {}
        self._audit_lock = RLock()

    async def delete_user(
        self,
        user_id: str,
        subject_hmac: str,
        request_id: str,
    ) -> DeletionReport:
        candidates = self.privacy_guard.subject_candidates(user_id)
        if subject_hmac not in candidates:
            raise ValueError("subject reference does not match user")
        with self._audit_lock:
            existing = self._audits.get(request_id)
            if existing is None:
                prior_request = next(
                    (
                        self._subject_audits[candidate]
                        for candidate in candidates
                        if candidate in self._subject_audits
                    ),
                    None,
                )
                existing = self._audits.get(prior_request) if prior_request else None
        if existing is None and self.database_url:
            existing = self._find_postgres_audit(candidates)
            if existing is not None:
                with self._audit_lock:
                    self._audits[existing.request_id] = existing
                    for candidate in existing.subject_candidates:
                        self._subject_audits[candidate] = existing.request_id
        if existing is not None:
            if existing.status == "redis_pending":
                await self._retry_audit(existing)
            return self._report(existing)

        redis_cleaned = self.runtime_state is None
        redis_prepared = self.runtime_state is None
        if self.runtime_state is not None:
            try:
                for candidate in candidates:
                    await self.runtime_state.begin_deletion(candidate)
                redis_prepared = True
            except Exception:
                redis_cleaned = False

        if self.database_url:
            counts = self._delete_postgres(user_id, candidates, request_id)
        else:
            counts = self._delete_memory(user_id)

        if self.runtime_state is not None:
            try:
                if not redis_prepared:
                    for candidate in candidates:
                        await self.runtime_state.begin_deletion(candidate)
                for candidate in candidates:
                    await self.runtime_state.delete_subject(candidate)
                redis_cleaned = True
            except Exception:
                redis_cleaned = False

        audit = _DeletionAudit(
            request_id=request_id,
            subject_hmac=subject_hmac,
            subject_candidates=candidates,
            status="completed" if redis_cleaned else "redis_pending",
            database_counts=counts,
        )
        with self._audit_lock:
            self._audits[request_id] = audit
            for candidate in candidates:
                self._subject_audits[candidate] = request_id
        if self.database_url:
            self._update_postgres_audit(audit)
        return self._report(audit)

    async def retry_pending(self, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("invalid retry limit")
        if self.database_url:
            for audit in self._load_postgres_pending(limit):
                with self._audit_lock:
                    self._audits.setdefault(audit.request_id, audit)
        with self._audit_lock:
            pending = [
                audit for audit in self._audits.values()
                if audit.status == "redis_pending"
            ][:limit]
        completed = 0
        for audit in pending:
            if await self._retry_audit(audit):
                completed += 1
        return completed

    async def _retry_audit(self, audit: _DeletionAudit) -> bool:
        if self.runtime_state is None:
            return False
        try:
            for candidate in audit.subject_candidates:
                await self.runtime_state.begin_deletion(candidate)
            for candidate in audit.subject_candidates:
                await self.runtime_state.delete_subject(candidate)
        except Exception:
            return False
        audit.status = "completed"
        if self.database_url:
            self._update_postgres_audit(audit)
        return True

    def _find_postgres_audit(
        self,
        candidates: tuple[str, ...],
    ) -> _DeletionAudit | None:
        import psycopg

        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            row = connection.execute(
                """
                SELECT request_id, subject_hmac, scope, database_counts, status
                FROM ai_privacy_deletion_audits
                WHERE subject_hmac = ANY(%s)
                ORDER BY requested_at DESC LIMIT 1
                """,
                (list(candidates),),
            ).fetchone()
        return self._audit_from_row(row) if row else None

    def _load_postgres_pending(self, limit: int) -> list[_DeletionAudit]:
        import psycopg

        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT request_id, subject_hmac, scope, database_counts, status
                FROM ai_privacy_deletion_audits
                WHERE status='redis_pending'
                ORDER BY requested_at LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    @staticmethod
    def _audit_from_row(row: Any) -> _DeletionAudit:
        scope = row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
        counts = row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
        candidates = tuple(scope.get("subject_hmacs") or [row[1]])
        return _DeletionAudit(
            request_id=row[0],
            subject_hmac=row[1],
            subject_candidates=candidates,
            status=row[4],
            database_counts={key: int(value) for key, value in counts.items()},
        )

    def pending_audits(self) -> list[dict[str, Any]]:
        with self._audit_lock:
            return [
                {
                    "request_id": audit.request_id,
                    "subject_hmac": audit.subject_hmac,
                    "status": audit.status,
                }
                for audit in self._audits.values()
                if audit.status == "redis_pending"
            ]

    def is_deleted(self, user_id: str) -> bool:
        if not self.database_url:
            return self.privacy_guard.is_tombstoned(user_id)
        import psycopg

        candidates = self.privacy_guard.subject_candidates(user_id)
        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            return bool(connection.execute(
                "SELECT 1 FROM ai_privacy_tombstones WHERE subject_hmac = ANY(%s) LIMIT 1",
                (list(candidates),),
            ).fetchone())

    def _delete_memory(self, user_id: str) -> dict[str, int]:
        with self.privacy_guard.memory_deletion(user_id):
            action_repository = getattr(self.action_service, "repository", None)
            reminder_repository = getattr(self.reminder_service, "repository", None)
            counts = {
                "memories": self.memory_service.purge_user(user_id),
                "actions": int(action_repository.delete_for_user(user_id))
                if action_repository is not None and hasattr(action_repository, "delete_for_user") else 0,
                "reminders": int(reminder_repository.delete_for_user(user_id))
                if reminder_repository is not None and hasattr(reminder_repository, "delete_for_user") else 0,
                "crisis_events": int(self.crisis_repository.delete_for_user(user_id))
                if self.crisis_repository is not None and hasattr(self.crisis_repository, "delete_for_user") else 0,
                "quality_logs": int(self.quality_service.delete_subject(
                    self.privacy_guard.subject_candidates(user_id)
                )) if self.quality_service is not None and hasattr(self.quality_service, "delete_subject") else 0,
                "lesson_candidates": int(self.lesson_repository.delete_subject(
                    self.privacy_guard.subject_candidates(user_id)
                )) if self.lesson_repository is not None and hasattr(self.lesson_repository, "delete_subject") else 0,
            }
            conversation_count = len(
                self.user_data_service.repository.list_conversation_events(user_id)
            )
            self.user_data_service.repository.delete_user(user_id)
            counts["conversation_events"] = conversation_count
            counts["users"] = 1
            return counts

    def _delete_postgres(
        self,
        user_id: str,
        candidates: tuple[str, ...],
        request_id: str,
    ) -> dict[str, int]:
        import hashlib
        import psycopg

        legacy_hash = hashlib.sha256(
            f"{self.legacy_quality_salt}:{user_id}".encode("utf-8")
        ).hexdigest()
        counts: dict[str, int] = {}
        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"privacy-subject:{len(user_id)}:{user_id}",),
            )
            for candidate in candidates:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("privacy-hmac:" + candidate,),
                )
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO ai_privacy_tombstones
                        (subject_hmac, status, created_at, updated_at)
                    VALUES (%s, 'deleting', now(), now())
                    ON CONFLICT (subject_hmac) DO UPDATE
                    SET status='deleting', updated_at=now()
                    """,
                    (candidate,),
                )

            def delete(name: str, statement: str, parameters: tuple[Any, ...]) -> None:
                counts[name] = connection.execute(statement, parameters).rowcount

            lesson_rows = connection.execute(
                "SELECT DISTINCT lesson_id FROM ai_lesson_subjects WHERE subject_hmac = ANY(%s)",
                (list(candidates),),
            ).fetchall()
            legacy_lesson_rows = connection.execute(
                """
                SELECT DISTINCT lesson_ref FROM ai_inspection_logs
                WHERE (subject_hmac = ANY(%s) OR user_hash=%s)
                  AND lesson_ref IS NOT NULL
                """,
                (list(candidates), legacy_hash),
            ).fetchall()
            lesson_ids = list({
                row[0] for row in (*lesson_rows, *legacy_lesson_rows) if row[0]
            })
            delete(
                "lesson_subjects",
                "DELETE FROM ai_lesson_subjects WHERE subject_hmac = ANY(%s)",
                (list(candidates),),
            )
            delete(
                "quality_logs",
                "DELETE FROM ai_inspection_logs WHERE subject_hmac = ANY(%s) OR user_hash=%s",
                (list(candidates), legacy_hash),
            )
            if lesson_ids:
                delete(
                    "lessons",
                    """
                    DELETE FROM ai_lessons
                    WHERE lesson_id = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1 FROM ai_lesson_subjects links
                          WHERE links.lesson_id=ai_lessons.lesson_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM ai_inspection_logs logs
                          WHERE logs.lesson_ref=ai_lessons.lesson_id
                      )
                    """,
                    (lesson_ids,),
                )
            else:
                counts["lessons"] = 0
            delete("memory_audits", "DELETE FROM ai_memory_audit WHERE user_id=%s", (user_id,))
            recommendation_rows = connection.execute(
                "SELECT recommendation_id FROM ai_action_recommendations WHERE user_id=%s",
                (user_id,),
            ).fetchall()
            recommendation_ids = [row[0] for row in recommendation_rows]
            if recommendation_ids:
                delete(
                    "action_events",
                    "DELETE FROM ai_action_events WHERE user_id=%s OR recommendation_id = ANY(%s)",
                    (user_id, recommendation_ids),
                )
            else:
                delete("action_events", "DELETE FROM ai_action_events WHERE user_id=%s", (user_id,))
            delete("actions", "DELETE FROM ai_action_recommendations WHERE user_id=%s", (user_id,))
            delete("memories", "DELETE FROM ai_memories WHERE user_id=%s", (user_id,))
            delete("reminders", "DELETE FROM ai_reminders WHERE user_id=%s", (user_id,))
            delete("crisis_events", "DELETE FROM ai_crisis_events WHERE user_id=%s", (user_id,))
            delete("conversation_events", "DELETE FROM ai_conversation_events WHERE user_id=%s", (user_id,))
            delete("consents", "DELETE FROM ai_consents WHERE user_id=%s", (user_id,))
            delete("audit_logs", "DELETE FROM ai_audit_logs WHERE user_id=%s", (user_id,))
            delete("users", "DELETE FROM ai_users WHERE user_id=%s", (user_id,))
            for candidate in candidates:
                connection.execute(
                    "UPDATE ai_privacy_tombstones SET status='deleted', updated_at=now() WHERE subject_hmac=%s",
                    (candidate,),
                )
            connection.execute(
                """
                INSERT INTO ai_privacy_deletion_audits
                    (request_id, subject_hmac, scope, database_counts, status,
                     requested_at, completed_at)
                VALUES (%s,%s,%s,%s,'redis_pending',now(),NULL)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    request_id,
                    candidates[0],
                    json.dumps({"subject_hmacs": list(candidates)}),
                    json.dumps(counts),
                ),
            )
        self.memory_service.purge_user(user_id)
        return counts

    def _update_postgres_audit(self, audit: _DeletionAudit) -> None:
        import psycopg

        with psycopg.connect(self.database_url, connect_timeout=5) as connection:
            connection.execute(
                """
                UPDATE ai_privacy_deletion_audits
                SET status=%s, completed_at=CASE WHEN %s='completed' THEN now() ELSE NULL END
                WHERE request_id=%s
                """,
                (audit.status, audit.status, audit.request_id),
            )

    @staticmethod
    def _report(audit: _DeletionAudit) -> DeletionReport:
        return DeletionReport(
            status=audit.status,
            subject_hmac=audit.subject_hmac,
            database_counts=dict(audit.database_counts),
            redis_cleaned=audit.status == "completed",
        )
