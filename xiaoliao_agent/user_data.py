from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
import uuid
from typing import Any


@dataclass
class UserRecord:
    user_id: str
    nickname: str
    birth_year: int | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass
class UserConsent:
    user_id: str
    personalization: bool
    sensitive: bool
    version: str
    granted_at: datetime | None
    revoked_at: datetime | None
    updated_at: datetime


@dataclass
class ConversationEvent:
    event_id: str
    user_id: str
    role: str
    content_ref: str
    intent: str
    request_id: str
    created_at: datetime


@dataclass
class AuditRecord:
    audit_id: str
    user_id: str
    action: str
    resource: str
    request_id: str
    created_at: datetime


class MemoryUserRepository:
    def __init__(self):
        self._users: dict[str, UserRecord] = {}
        self._consents: dict[str, UserConsent] = {}
        self._events: dict[str, ConversationEvent] = {}
        self._audits: dict[str, AuditRecord] = {}
        self._lock = Lock()

    def get_or_create_user(self, user_id: str, *, nickname: str = "", birth_year: int | None = None) -> UserRecord:
        now = datetime.now(timezone.utc)
        with self._lock:
            user = self._users.get(user_id)
            if user is not None:
                return user
            user = UserRecord(
                user_id=user_id,
                nickname=nickname,
                birth_year=birth_year,
                status="active",
                created_at=now,
                updated_at=now,
            )
            self._users[user_id] = user
            return user

    def set_consent(
        self,
        user_id: str,
        *,
        personalization: bool,
        sensitive: bool = False,
        version: str = "v1",
    ) -> UserConsent:
        now = datetime.now(timezone.utc)
        with self._lock:
            current = self._consents.get(user_id)
            granted_at = current.granted_at if current and current.granted_at else None
            revoked_at = current.revoked_at if current and current.revoked_at else None
            if personalization:
                granted_at = granted_at or now
                revoked_at = None
            else:
                granted_at = None
                revoked_at = revoked_at or now
            consent = UserConsent(
                user_id=user_id,
                personalization=personalization,
                sensitive=sensitive if personalization else False,
                version=version,
                granted_at=granted_at,
                revoked_at=revoked_at,
                updated_at=now,
            )
            self._consents[user_id] = consent
            return consent

    def get_consent(self, user_id: str) -> tuple[bool, bool]:
        with self._lock:
            consent = self._consents.get(user_id)
            return (consent.personalization, consent.sensitive) if consent else (False, False)

    def log_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        with self._lock:
            self._events[event.event_id] = event
            return event

    def log_audit(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            self._audits[record.audit_id] = record
            return record

    def list_conversation_events(self, user_id: str, limit: int = 50) -> list[ConversationEvent]:
        with self._lock:
            events = [
                event for event in self._events.values()
                if event.user_id == user_id
            ]
            return sorted(events, key=lambda item: item.created_at, reverse=True)[:limit]

    def delete_user(self, user_id: str) -> None:
        with self._lock:
            self._users.pop(user_id, None)
            self._consents.pop(user_id, None)
            self._events = {
                key: event
                for key, event in self._events.items()
                if event.user_id != user_id
            }
            self._audits = {
                key: record
                for key, record in self._audits.items()
                if record.user_id != user_id
            }


class PostgresUserRepository:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)

    def get_or_create_user(self, user_id: str, *, nickname: str = "", birth_year: int | None = None) -> UserRecord:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_users (user_id, nickname, birth_year, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'active', now(), now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, nickname, birth_year),
            )
            row = connection.execute(
                "SELECT user_id, nickname, birth_year, status, created_at, updated_at FROM ai_users WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        return UserRecord(*row)

    def set_consent(
        self,
        user_id: str,
        *,
        personalization: bool,
        sensitive: bool = False,
        version: str = "v1",
    ) -> UserConsent:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO ai_consents
                    (user_id, personalization, sensitive, version, granted_at, revoked_at, updated_at)
                VALUES (%s, %s, %s, %s,
                        CASE WHEN %s THEN now() ELSE NULL END,
                        CASE WHEN %s THEN NULL ELSE now() END,
                        now())
                ON CONFLICT (user_id) DO UPDATE SET
                    personalization = EXCLUDED.personalization,
                    sensitive = EXCLUDED.sensitive,
                    version = EXCLUDED.version,
                    granted_at = CASE
                        WHEN EXCLUDED.personalization THEN
                            COALESCE(ai_consents.granted_at, now())
                        ELSE NULL
                    END,
                    revoked_at = CASE
                        WHEN EXCLUDED.personalization THEN NULL
                        ELSE COALESCE(ai_consents.revoked_at, now())
                    END,
                    updated_at = now()
                RETURNING user_id, personalization, sensitive, version, granted_at, revoked_at, updated_at
                """,
                (user_id, personalization, sensitive, version, personalization, personalization),
            ).fetchone()
        return UserConsent(*row)

    def get_consent(self, user_id: str) -> tuple[bool, bool]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT personalization, sensitive FROM ai_consents WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def log_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_conversation_events
                    (event_id, user_id, role, content_ref, intent, request_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (event.event_id, event.user_id, event.role, event.content_ref,
                 event.intent, event.request_id, event.created_at),
            )
        return event

    def log_audit(self, record: AuditRecord) -> AuditRecord:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_audit_logs (audit_id, user_id, action, resource, request_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (record.audit_id, record.user_id, record.action, record.resource,
                 record.request_id, record.created_at),
            )
        return record

    def list_conversation_events(self, user_id: str, limit: int = 50) -> list[ConversationEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, user_id, role, content_ref, intent, request_id, created_at
                FROM ai_conversation_events
                WHERE user_id=%s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
        return [ConversationEvent(*row) for row in rows]

    def delete_user(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ai_audit_logs WHERE user_id=%s", (user_id,))
            connection.execute("DELETE FROM ai_users WHERE user_id=%s", (user_id,))


class UserDataService:
    def __init__(
        self,
        repository: MemoryUserRepository | PostgresUserRepository,
        *,
        memory_service: Any | None = None,
    ):
        self.repository = repository
        self.memory_service = memory_service

    def get_or_create_user(self, user_id: str, **kwargs) -> UserRecord:
        return self.repository.get_or_create_user(user_id, **kwargs)

    def set_consent(self, user_id: str, *, personalization: bool, sensitive: bool = False) -> UserConsent:
        consent = self.repository.set_consent(
            user_id,
            personalization=personalization,
            sensitive=sensitive,
        )
        if self.memory_service is not None:
            self.memory_service.set_consent(
                user_id,
                personalization=personalization,
                sensitive=sensitive,
            )
        return consent

    def consent_for(self, user_id: str) -> tuple[bool, bool]:
        return self.repository.get_consent(user_id)

    def log_conversation_pair(
        self,
        user_id: str,
        *,
        user_text: str,
        reply: str,
        intent: str,
        request_id: str,
    ) -> None:
        self.get_or_create_user(user_id)
        now = datetime.now(timezone.utc)
        self.repository.log_conversation_event(ConversationEvent(
            event_id=uuid.uuid4().hex,
            user_id=user_id,
            role="user",
            content_ref=self._content_ref(user_text),
            intent=intent,
            request_id=request_id,
            created_at=now,
        ))
        self.repository.log_conversation_event(ConversationEvent(
            event_id=uuid.uuid4().hex,
            user_id=user_id,
            role="assistant",
            content_ref=self._content_ref(reply),
            intent=intent,
            request_id=request_id,
            created_at=now,
        ))

    def log_audit(self, user_id: str, *, action: str, resource: str, request_id: str) -> AuditRecord:
        return self.repository.log_audit(AuditRecord(
            audit_id=uuid.uuid4().hex,
            user_id=user_id,
            action=action,
            resource=resource,
            request_id=request_id,
            created_at=datetime.now(timezone.utc),
        ))

    def summary(self, user_id: str) -> dict[str, Any]:
        self.get_or_create_user(user_id)
        personalization, sensitive = self.consent_for(user_id)
        profile = []
        if self.memory_service is not None:
            profile = [
                record.content
                for record in self.memory_service.view(user_id)
                if record.memory_type in {"profile", "family_relationship", "interest_preference"}
            ]
        events = [
            {
                "role": event.role,
                "content_ref": event.content_ref,
                "intent": event.intent,
                "request_id": event.request_id,
                "created_at": event.created_at.isoformat(),
            }
            for event in self.repository.list_conversation_events(user_id, limit=20)
        ]
        return {
            "user_id": user_id,
            "personalization": personalization,
            "sensitive": sensitive,
            "profile": profile,
            "recent_events": events,
        }

    def delete_user(self, user_id: str) -> None:
        if self.memory_service is not None:
            self.memory_service.delete_all(user_id, hard=True)
            self.memory_service.set_consent(user_id, personalization=False)
        self.repository.delete_user(user_id)

    @staticmethod
    def _content_ref(content: str) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
