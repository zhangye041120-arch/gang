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
