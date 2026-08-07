from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from threading import Lock
from typing import Any
import uuid

from .memory_repository import MemoryMemoryRepository, MemoryRecord, MemoryVectorIndex


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
    ):
        self.repository = repository
        self.vector_index = vector_index or MemoryVectorIndex()
        self.embed_client = embed_client
        self.confidence_threshold = confidence_threshold
        self.default_limit = default_limit
        self.default_max_chars = default_max_chars
        self.vector_min_score = vector_min_score
        self.vector_search_fallback = vector_search_fallback
        self._cache: dict[str, str] = {}
        self._lock = Lock()

    def set_consent(self, user_id: str, *, personalization: bool, sensitive: bool = False) -> None:
        if not user_id.strip():
            raise ValueError("user_id is required")
        self.repository.set_consent(user_id, personalization, sensitive if personalization else False)
        with self._lock:
            self._cache.pop(user_id, None)

    def consent_for(self, user_id: str) -> ConsentState:
        personalization, sensitive = self.repository.get_consent(user_id)
        return ConsentState(personalization, sensitive)

    def save_candidate(self, user_id: str, candidate: MemoryCandidate) -> MemoryRecord:
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
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
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
        )
        saved = self.repository.upsert(record)
        self._invalidate(user_id, saved.memory_id)
        return saved

    def view(self, user_id: str) -> list[MemoryRecord]:
        return self.repository.list_for_user(user_id)

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
        if not self.consent_for(user_id).personalization:
            return ""
        limit = self.default_limit if limit is None else limit
        max_chars = self.default_max_chars if max_chars is None else max_chars
        cache_key = f"{user_id}:{query}" if query else user_id
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
        lines = [f"{item.content} [{item.memory_type}]" for item in records]
        context = "\n".join(lines)[:max_chars]
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
        record = self.repository.correct(user_id, memory_id, cleaned, self._hash(cleaned))
        self._invalidate(user_id, memory_id)
        return record

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
