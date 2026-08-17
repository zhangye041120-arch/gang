from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import nullcontext
import hashlib
import json
from threading import Lock
from typing import Any
import uuid



MEMORY_TYPES = frozenset({
    "profile",
    "family_relationship",
    "interest_preference",
    "key_event",
    "emotion_trend",
    "action_summary",
    "conceptualization_clue",
})


class EmbeddingClientProtocol:
    """Minimal protocol so MemoryService doesn't import embeddings.py at module level."""
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class ConsentRequiredError(PermissionError):
    pass


class SensitiveConsentRequiredError(PermissionError):
    pass


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    content: str
    confidence: float
    source_message_id: str
    consent_scope: str = "personalization"
    explicitly_stated: bool = False
    sensitive: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    memory_key: str = ""
    source_type: str = "conversation"
    supersedes_memory_id: str | None = None


@dataclass(frozen=True)
class ConsentState:
    personalization: bool = False
    sensitive: bool = False


class MemoryService:
    def __init__(
        self,
        repository,
        *,
        vector_index: MemoryVectorIndex | None = None,
        embed_client: Any = None,
        confidence_threshold: float = 0.8,
        default_limit: int = 6,
        default_max_chars: int = 2000,
        vector_min_score: float = 0.55,
        vector_search_fallback: bool = True,
        cache_enabled: bool = True,
    ):
        self.repository = repository
        self.vector_index = vector_index or MemoryVectorIndex()
        self.embed_client = embed_client
        self.confidence_threshold = confidence_threshold
        self.default_limit = default_limit
        self.default_max_chars = default_max_chars
        self.vector_min_score = vector_min_score
        self.vector_search_fallback = vector_search_fallback
        self.cache_enabled = cache_enabled
        self._cache: dict[str, str] = {}
        self._lock = Lock()

    def _privacy_write(self, user_id: str):
        guard = getattr(self.repository, "privacy_guard", None)
        return guard.memory_write(user_id) if guard is not None else nullcontext()

    def set_consent(self, user_id: str, *, personalization: bool, sensitive: bool = False) -> None:
        if not user_id.strip():
            raise ValueError("user_id is required")
        with self._privacy_write(user_id):
            self.repository.set_consent(user_id, personalization, sensitive if personalization else False)
        with self._lock:
            self._cache.pop(user_id, None)

    def consent_for(self, user_id: str) -> ConsentState:
        personalization, sensitive = self.repository.get_consent(user_id)
        return ConsentState(personalization, sensitive)

    def save_candidate(self, user_id: str, candidate: MemoryCandidate) -> MemoryRecord:
        with self._privacy_write(user_id):
            record = self._build_record(user_id, candidate)
            saved = self.repository.upsert(record)
        self._invalidate(user_id, saved.memory_id)
        return saved

    def save_versioned_candidate(
        self,
        user_id: str,
        candidate: MemoryCandidate,
    ) -> MemoryRecord:
        with self._privacy_write(user_id):
            if not candidate.memory_key.strip() or len(candidate.memory_key) > 128:
                raise ValueError("memory_key is required")
            record = self._build_record(user_id, candidate)
            saved = self.repository.save_versioned(record)
        self._invalidate(user_id, saved.memory_id)
        return saved

    def _build_record(
        self,
        user_id: str,
        candidate: MemoryCandidate,
    ) -> MemoryRecord:
        consent = self.consent_for(user_id)
        if not consent.personalization:
            raise ConsentRequiredError("personalization consent is required")
        if candidate.sensitive and not consent.sensitive:
            raise SensitiveConsentRequiredError("sensitive memory consent is required")
        if candidate.memory_type not in MEMORY_TYPES:
            raise ValueError("invalid memory type")
        content = candidate.content.strip()
        if not content or len(content) > 1000:
            raise ValueError("memory content length is invalid")
        if not 0 <= candidate.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not candidate.explicitly_stated and candidate.confidence < self.confidence_threshold:
            raise ValueError("inferred candidate confidence is too low")
        if not candidate.source_message_id.strip():
            raise ValueError("source_message_id is required")
        if candidate.consent_scope != "personalization":
            raise ValueError("invalid consent scope")
        if candidate.source_type not in {
            "conversation", "explicit", "correction", "action_event"
        }:
            raise ValueError("invalid source type")
        now = datetime.now(timezone.utc)
        return MemoryRecord(
            memory_id=uuid.uuid4().hex,
            user_id=user_id,
            memory_type=candidate.memory_type,
            content=content,
            content_hash=self._hash(content),
            confidence=candidate.confidence,
            source_message_id=candidate.source_message_id,
            consent_scope=candidate.consent_scope,
            valid_from=candidate.valid_from or now,
            valid_until=candidate.valid_until,
            created_at=now,
            updated_at=now,
            sensitive=candidate.sensitive,
            memory_key=candidate.memory_key.strip(),
            source_type=candidate.source_type,
            supersedes_memory_id=candidate.supersedes_memory_id,
        )

    def view(self, user_id: str) -> list[MemoryRecord]:
        return self.list_current(user_id)

    def list_current(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid pagination")
        consent = self.consent_for(user_id)
        if not consent.personalization:
            return []
        current = [
            record
            for record in self.repository.list_for_user(user_id)
            if consent.sensitive or not record.sensitive
        ]
        return current[offset:offset + limit]

    def get_context(
        self,
        user_id: str,
        *,
        query: str = "",
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        """Return user memory context for the current conversation turn.

        When *query* is non-empty and an embedding client is wired, semantic
        vector search is used.  Otherwise the most recent memories are returned
        in update order (lexical fallback).
        """
        consent = self.consent_for(user_id)
        if not consent.personalization:
            return ""
        limit = self.default_limit if limit is None else limit
        max_chars = self.default_max_chars if max_chars is None else max_chars
        cache_key = f"{user_id}:{query}" if query else user_id
        if self.cache_enabled:
            with self._lock:
                if cache_key in self._cache:
                    return self._cache[cache_key][:max_chars]
        records: list[MemoryRecord] = []
        if query and self.embed_client is not None:
            try:
                vector = self.embed_client.embed([query])[0]
                mem_ids = self.repository.vector_search(user_id, vector, limit * 2)
                records = [
                    self.repository.get(user_id, mem_id)
                    for mem_id, score in mem_ids
                    if score >= self.vector_min_score
                ]
            except Exception:
                if not self.vector_search_fallback:
                    records = []
        if not records:
            records = self.repository.list_for_user(user_id)[:limit]
        else:
            records = records[:limit]
        if not consent.sensitive:
            records = [record for record in records if not record.sensitive]
        lines = [f"{item.content} [{item.memory_type}]" for item in records]
        context = "\n".join(lines)[:max_chars]
        if self.cache_enabled:
            with self._lock:
                self._cache[cache_key] = context
        return context

    def backfill_embedding(self, user_id: str, memory_id: str, vector: list[float]) -> None:
        """Persist an embedding vector for one memory row."""
        self.repository.update_embedding(memory_id, vector)
        self._invalidate(user_id, memory_id)

    def correct(self, user_id: str, memory_id: str, content: str) -> MemoryRecord:
        cleaned = content.strip()
        if not cleaned or len(cleaned) > 1000:
            raise ValueError("memory content length is invalid")
        original = self.repository.get(user_id, memory_id)
        if original.memory_key:
            return self.save_versioned_candidate(
                user_id,
                MemoryCandidate(
                    memory_type=original.memory_type,
                    content=cleaned,
                    confidence=1.0,
                    source_message_id=f"correction:{uuid.uuid4().hex}",
                    consent_scope=original.consent_scope,
                    explicitly_stated=True,
                    sensitive=original.sensitive,
                    memory_key=original.memory_key,
                    source_type="correction",
                    supersedes_memory_id=original.memory_id,
                ),
            )
        record = self.repository.correct(user_id, memory_id, cleaned, self._hash(cleaned))
        self._invalidate(user_id, memory_id)
        return record

    def delete_one(self, user_id: str, memory_id: str) -> bool:
        try:
            self.repository.get(user_id, memory_id)
        except KeyError:
            return False
        self.repository.soft_delete(user_id, memory_id)
        self._invalidate(user_id, memory_id)
        return True

    def soft_delete(self, user_id: str, memory_id: str) -> None:
        self.repository.soft_delete(user_id, memory_id)
        self._invalidate(user_id, memory_id)

    def hard_delete(self, user_id: str, memory_id: str) -> None:
        self.repository.hard_delete(user_id, memory_id)
        self._invalidate(user_id, memory_id)

    def delete_all(self, user_id: str, *, hard: bool = True) -> None:
        for record in self.repository.list_for_user(user_id, include_deleted=True):
            if hard:
                self.hard_delete(user_id, record.memory_id)
            else:
                self.soft_delete(user_id, record.memory_id)

    def purge_user(self, user_id: str) -> int:
        records = self.repository.list_for_user(user_id, include_deleted=True)
        memory_ids = [record.memory_id for record in records]
        purge = getattr(self.repository, "purge_user", None)
        if purge is not None:
            count = int(purge(user_id))
        else:
            for memory_id in memory_ids:
                self.repository.hard_delete(user_id, memory_id)
            count = len(memory_ids)
        for memory_id in memory_ids:
            self.vector_index.delete(memory_id)
        self._invalidate(user_id, "")
        return count

    def cache_contains(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._cache

    def _invalidate(self, user_id: str, memory_id: str) -> None:
        with self._lock:
            stale = [key for key in self._cache if key == user_id or key.startswith(f"{user_id}:")]
            for key in stale:
                self._cache.pop(key, None)
        self.vector_index.delete(memory_id)

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock


@dataclass
class MemoryRecord:
    memory_id: str
    user_id: str
    memory_type: str
    content: str
    content_hash: str
    confidence: float
    source_message_id: str
    consent_scope: str
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    sensitive: bool = False
    memory_key: str = ""
    supersedes_memory_id: str | None = None
    source_type: str = "conversation"


@dataclass(frozen=True)
class MemoryAudit:
    audit_id: str
    user_id: str
    memory_id: str
    action: str
    created_at: str


class MemoryVectorIndex:
    def __init__(self):
        self._vectors: dict[str, list[float]] = {}
        self._lock = Lock()

    def put(self, memory_id: str, vector: list[float]) -> None:
        with self._lock:
            self._vectors[memory_id] = list(vector)

    def delete(self, memory_id: str) -> None:
        with self._lock:
            self._vectors.pop(memory_id, None)

    def contains(self, memory_id: str) -> bool:
        with self._lock:
            return memory_id in self._vectors


class MemoryMemoryRepository:
    def __init__(self, *, privacy_guard: Any | None = None):
        self._rows: dict[str, MemoryRecord] = {}
        self._lock = Lock()
        self.audit_log: list[MemoryAudit] = []
        self._consents: dict[str, tuple[bool, bool]] = {}
        self.privacy_guard = privacy_guard

    def _write(self, user_id: str):
        return (
            self.privacy_guard.memory_write(user_id)
            if self.privacy_guard is not None
            else nullcontext()
        )

    def set_consent(self, user_id: str, personalization: bool, sensitive: bool) -> None:
        with self._write(user_id), self._lock:
            self._consents[user_id] = (personalization, sensitive)

    def get_consent(self, user_id: str) -> tuple[bool, bool]:
        with self._lock:
            return self._consents.get(user_id, (False, False))

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        with self._write(record.user_id), self._lock:
            for current in self._rows.values():
                if current.user_id != record.user_id or current.deleted_at is not None:
                    continue
                if (current.memory_type, current.source_message_id, current.consent_scope) == (
                    record.memory_type,
                    record.source_message_id,
                    record.consent_scope,
                ):
                    current.content = record.content
                    current.content_hash = record.content_hash
                    current.confidence = record.confidence
                    current.valid_from = record.valid_from
                    current.valid_until = record.valid_until
                    current.updated_at = record.updated_at
                    current.sensitive = record.sensitive
                    return current
                if current.memory_type == record.memory_type and current.content_hash == record.content_hash:
                    return current
            self._rows[record.memory_id] = record
            return record

    def save_versioned(self, record: MemoryRecord) -> MemoryRecord:
        now = datetime.now(timezone.utc)
        with self._write(record.user_id), self._lock:
            current = next((
                item for item in self._rows.values()
                if item.user_id == record.user_id
                and item.memory_key == record.memory_key
                and item.deleted_at is None
                and item.valid_until is None
            ), None)
            if current is not None and current.content_hash == record.content_hash:
                return current
            if current is not None:
                current.valid_until = now
                current.updated_at = now
                record.supersedes_memory_id = current.memory_id
            self._rows[record.memory_id] = record
            return record

    def replace(self, record: MemoryRecord) -> None:
        with self._lock:
            self._rows[record.memory_id] = record

    def get(self, user_id: str, memory_id: str, *, include_deleted: bool = False) -> MemoryRecord:
        with self._lock:
            record = self._rows.get(memory_id)
            if record is None or record.user_id != user_id or (record.deleted_at and not include_deleted):
                raise KeyError("memory not found")
            return record

    def list_for_user(self, user_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = [
                item for item in self._rows.values()
                if item.user_id == user_id
                and (include_deleted or item.deleted_at is None)
                and (include_deleted or item.valid_from <= now)
                and (include_deleted or item.valid_until is None or item.valid_until > now)
            ]
            return sorted(rows, key=lambda item: item.updated_at, reverse=True)

    def correct(self, user_id: str, memory_id: str, content: str, content_hash: str) -> MemoryRecord:
        with self._lock:
            record = self._rows.get(memory_id)
            if record is None or record.user_id != user_id or record.deleted_at:
                raise KeyError("memory not found")
            record.content = content
            record.content_hash = content_hash
            record.updated_at = datetime.now(timezone.utc)
            self._audit(user_id, memory_id, "correct")
            return record

    def soft_delete(self, user_id: str, memory_id: str) -> None:
        with self._lock:
            record = self._rows.get(memory_id)
            if record is None or record.user_id != user_id:
                return
            if record.deleted_at is None:
                record.content = ""
                record.content_hash = ""
                record.deleted_at = datetime.now(timezone.utc)
                record.updated_at = record.deleted_at
                self._audit(user_id, memory_id, "soft_delete")

    def hard_delete(self, user_id: str, memory_id: str) -> None:
        with self._lock:
            record = self._rows.get(memory_id)
            if record is None or record.user_id != user_id:
                return
            del self._rows[memory_id]
            self._audit(user_id, memory_id, "hard_delete")

    def _audit(self, user_id: str, memory_id: str, action: str) -> None:
        import uuid

        self.audit_log.append(MemoryAudit(
            audit_id=uuid.uuid4().hex,
            user_id=user_id,
            memory_id=memory_id,
            action=action,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))

    def purge_user(self, user_id: str) -> int:
        with self._lock:
            memory_ids = [
                memory_id for memory_id, record in self._rows.items()
                if record.user_id == user_id
            ]
            for memory_id in memory_ids:
                self._rows.pop(memory_id, None)
            self._consents.pop(user_id, None)
            self.audit_log = [
                audit for audit in self.audit_log if audit.user_id != user_id
            ]
            return len(memory_ids)


class PostgresMemoryRepository:
    COLUMNS = (
        "memory_id, user_id, memory_type, content, content_hash, confidence, "
        "source_message_id, consent_scope, valid_from, valid_until, created_at, updated_at, deleted_at, sensitive, "
        "memory_key, supersedes_memory_id, source_type"
    )

    def __init__(self, database_url: str, *, privacy_guard: Any | None = None):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)
        self.privacy_guard = privacy_guard

    def _protect(self, connection: Any, user_id: str) -> None:
        guard = getattr(self, "privacy_guard", None)
        if guard is not None:
            guard.protect_postgres_write(connection, user_id)

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(*row)

    def set_consent(self, user_id: str, personalization: bool, sensitive: bool) -> None:
        with self._connect() as connection:
            self._protect(connection, user_id)
            connection.execute(
                """
                INSERT INTO ai_users
                    (user_id, nickname, birth_year, status, created_at, updated_at)
                VALUES (%s, '', NULL, 'active', now(), now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id,),
            )
            connection.execute(
                """
                INSERT INTO ai_consents
                    (user_id, personalization, sensitive, version, granted_at, revoked_at, updated_at)
                VALUES (%s, %s, %s, 'v1',
                        CASE WHEN %s THEN now() ELSE NULL END,
                        CASE WHEN %s THEN NULL ELSE now() END,
                        now())
                ON CONFLICT (user_id) DO UPDATE SET
                    personalization = EXCLUDED.personalization,
                    sensitive = EXCLUDED.sensitive,
                    granted_at = CASE
                        WHEN EXCLUDED.personalization THEN COALESCE(ai_consents.granted_at, now())
                        ELSE NULL
                    END,
                    revoked_at = CASE
                        WHEN EXCLUDED.personalization THEN NULL
                        ELSE COALESCE(ai_consents.revoked_at, now())
                    END,
                    updated_at = now()
                """,
                (user_id, personalization, sensitive, personalization, personalization),
            )

    def get_consent(self, user_id: str) -> tuple[bool, bool]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT personalization, sensitive FROM ai_consents WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        with self._connect() as connection:
            self._protect(connection, record.user_id)
            source_row = connection.execute(
                f"SELECT {self.COLUMNS} FROM ai_memories WHERE user_id=%s AND memory_type=%s "
                "AND source_message_id=%s AND consent_scope=%s AND deleted_at IS NULL",
                (record.user_id, record.memory_type, record.source_message_id, record.consent_scope),
            ).fetchone()
            if source_row:
                memory_id = source_row[0]
                row = connection.execute(
                    f"UPDATE ai_memories SET content=%s, content_hash=%s, confidence=%s, valid_from=%s, "
                    f"valid_until=%s, updated_at=%s, embedding=NULL, sensitive=%s, memory_key=%s, "
                    f"source_type=%s WHERE memory_id=%s RETURNING {self.COLUMNS}",
                    (record.content, record.content_hash, record.confidence, record.valid_from,
                     record.valid_until, record.updated_at, record.sensitive,
                     record.memory_key or None, record.source_type, memory_id),
                ).fetchone()
                return self._record(row)
            duplicate = connection.execute(
                f"SELECT {self.COLUMNS} FROM ai_memories WHERE user_id=%s AND memory_type=%s "
                "AND content_hash=%s AND deleted_at IS NULL",
                (record.user_id, record.memory_type, record.content_hash),
            ).fetchone()
            if duplicate:
                return self._record(duplicate)
            row = connection.execute(
                f"INSERT INTO ai_memories ({self.COLUMNS}) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                f"RETURNING {self.COLUMNS}",
                (record.memory_id, record.user_id, record.memory_type, record.content, record.content_hash,
                 record.confidence, record.source_message_id, record.consent_scope, record.valid_from,
                 record.valid_until, record.created_at, record.updated_at, record.deleted_at,
                 record.sensitive, record.memory_key or None,
                 record.supersedes_memory_id, record.source_type),
            ).fetchone()
            return self._record(row)

    def save_versioned(self, record: MemoryRecord) -> MemoryRecord:
        with self._connect() as connection:
            self._protect(connection, record.user_id)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{len(record.user_id)}:{record.user_id}{record.memory_key}",),
            )
            current = connection.execute(
                f"SELECT {self.COLUMNS} FROM ai_memories "
                "WHERE user_id=%s AND memory_key=%s AND deleted_at IS NULL "
                "AND valid_until IS NULL FOR UPDATE",
                (record.user_id, record.memory_key),
            ).fetchone()
            if current and current[4] == record.content_hash:
                return self._record(current)
            if current:
                connection.execute(
                    "UPDATE ai_memories SET valid_until=now(), updated_at=now() "
                    "WHERE memory_id=%s",
                    (current[0],),
                )
                record.supersedes_memory_id = current[0]
            row = connection.execute(
                f"INSERT INTO ai_memories ({self.COLUMNS}) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                f"RETURNING {self.COLUMNS}",
                (
                    record.memory_id, record.user_id, record.memory_type,
                    record.content, record.content_hash, record.confidence,
                    record.source_message_id, record.consent_scope,
                    record.valid_from, record.valid_until, record.created_at,
                    record.updated_at, record.deleted_at, record.sensitive,
                    record.memory_key, record.supersedes_memory_id,
                    record.source_type,
                ),
            ).fetchone()
            return self._record(row)

    def replace(self, record: MemoryRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE ai_memories SET valid_until=%s, updated_at=%s WHERE memory_id=%s AND user_id=%s",
                (record.valid_until, record.updated_at, record.memory_id, record.user_id),
            )

    def get(self, user_id: str, memory_id: str, *, include_deleted: bool = False) -> MemoryRecord:
        suffix = "" if include_deleted else " AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {self.COLUMNS} FROM ai_memories WHERE user_id=%s AND memory_id=%s{suffix}",
                (user_id, memory_id),
            ).fetchone()
        if not row:
            raise KeyError("memory not found")
        return self._record(row)

    def list_for_user(self, user_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        where = "user_id=%s"
        if not include_deleted:
            where += " AND deleted_at IS NULL AND valid_from <= now() AND (valid_until IS NULL OR valid_until > now())"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self.COLUMNS} FROM ai_memories WHERE {where} ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def correct(self, user_id: str, memory_id: str, content: str, content_hash: str) -> MemoryRecord:
        with self._connect() as connection:
            self._protect(connection, user_id)
            row = connection.execute(
                f"UPDATE ai_memories SET content=%s, content_hash=%s, embedding=NULL, updated_at=now() "
                f"WHERE user_id=%s AND memory_id=%s AND deleted_at IS NULL RETURNING {self.COLUMNS}",
                (content, content_hash, user_id, memory_id),
            ).fetchone()
            if not row:
                raise KeyError("memory not found")
            self._audit(connection, user_id, memory_id, "correct")
            return self._record(row)

    def soft_delete(self, user_id: str, memory_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE ai_memories SET content='', content_hash='', embedding=NULL, deleted_at=now(), updated_at=now() "
                "WHERE user_id=%s AND memory_id=%s AND deleted_at IS NULL RETURNING memory_id",
                (user_id, memory_id),
            ).fetchone()
            if row:
                self._audit(connection, user_id, memory_id, "soft_delete")

    def hard_delete(self, user_id: str, memory_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "DELETE FROM ai_memories WHERE user_id=%s AND memory_id=%s RETURNING memory_id",
                (user_id, memory_id),
            ).fetchone()
            if row:
                self._audit(connection, user_id, memory_id, "hard_delete")

    def update_embedding(self, memory_id: str, vector: list[float]) -> None:
        import json

        with self._connect() as connection:
            connection.execute(
                "UPDATE ai_memories SET embedding=%s::vector WHERE memory_id=%s AND deleted_at IS NULL",
                (json.dumps(vector, separators=(",", ":")), memory_id),
            )

    def vector_search(
        self,
        user_id: str,
        vector: list[float],
        top_k: int = 6,
    ) -> list[tuple[str, float]]:
        """Return (memory_id, score) ordered by cosine similarity descending."""
        import json

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, 1 - (embedding <=> %s::vector) AS score
                FROM ai_memories
                WHERE user_id = %s
                  AND embedding IS NOT NULL
                  AND deleted_at IS NULL
                  AND valid_from <= now()
                  AND (valid_until IS NULL OR valid_until > now())
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (json.dumps(vector, separators=(",", ":")), user_id,
                 json.dumps(vector, separators=(",", ":")), top_k),
            ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]

    def find_embeddings_missing(self, limit: int = 500) -> list[str]:
        """Return memory_ids that still need an embedding vector."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT memory_id FROM ai_memories WHERE embedding IS NULL AND deleted_at IS NULL LIMIT %s",
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def count_embeddings(self) -> tuple[int, int]:
        """Return (with_embedding, without_embedding) for active memories."""
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM ai_memories WHERE deleted_at IS NULL"
            ).fetchone()[0]
            with_emb = connection.execute(
                "SELECT COUNT(*) FROM ai_memories WHERE embedding IS NOT NULL AND deleted_at IS NULL"
            ).fetchone()[0]
        return (with_emb, max(0, total - with_emb))

    @staticmethod
    def _audit(connection, user_id: str, memory_id: str, action: str) -> None:
        import uuid

        connection.execute(
            "INSERT INTO ai_memory_audit (audit_id, user_id, memory_id, action, created_at) VALUES (%s,%s,%s,%s,now())",
            (uuid.uuid4().hex, user_id, memory_id, action),
        )
