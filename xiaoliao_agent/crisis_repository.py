from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import uuid


@dataclass
class CrisisEvent:
    event_id: str
    user_id: str
    session_id: str
    risk_level: str
    evidence_code: str
    route: str
    status: str
    prompt_version: str
    response_version: str
    created_at: str
    reviewed_by: str = ""
    reviewed_at: str | None = None


def new_crisis_event(
    *,
    user_id: str,
    session_id: str,
    evidence_code: str,
    route: str,
    prompt_version: str,
    response_version: str,
) -> CrisisEvent:
    return CrisisEvent(
        event_id=uuid.uuid4().hex,
        user_id=user_id,
        session_id=session_id,
        risk_level="crisis",
        evidence_code=evidence_code,
        route=route,
        status="pending",
        prompt_version=prompt_version,
        response_version=response_version,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


class CrisisEventRepository(Protocol):
    def add(self, event: CrisisEvent) -> None:
        ...


class MemoryCrisisEventRepository:
    def __init__(self):
        self._events: dict[str, CrisisEvent] = {}

    def add(self, event: CrisisEvent) -> None:
        self._events.setdefault(event.event_id, event)

    def list_events(self) -> list[CrisisEvent]:
        return list(self._events.values())


class PostgresCrisisEventRepository:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - integration dependency
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)

    def add(self, event: CrisisEvent) -> None:  # pragma: no cover - covered by integration test
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_crisis_events
                    (event_id, user_id, session_id, risk_level, evidence_code, route,
                     status, prompt_version, response_version, created_at, reviewed_by, reviewed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.user_id,
                    event.session_id,
                    event.risk_level,
                    event.evidence_code,
                    event.route,
                    event.status,
                    event.prompt_version,
                    event.response_version,
                    event.created_at,
                    event.reviewed_by or None,
                    event.reviewed_at,
                ),
            )
