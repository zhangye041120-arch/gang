from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from threading import Lock
from typing import Any
import json
import uuid

from .content_refs import HmacReferenceService
from .privacy import SubjectDeletedError
from .memory import (
    ConsentRequiredError,
    MemoryCandidate,
    MemoryService,
    SensitiveConsentRequiredError,
)
from .api_contract import ActionPayload


class ActionContractError(ValueError):
    pass


class ActionAlreadyRecommended(ActionContractError):
    pass


class ActionEventConflict(ActionContractError):
    pass


class ActionMemoryUnavailable(RuntimeError):
    pass


@dataclass
class ActionRecommendation:
    recommendation_id: str
    user_id: str
    session_id: str
    module: str
    action_json: dict[str, Any]
    reason: str
    status: str
    source_message_id: str
    expires_at: datetime | None
    created_at: datetime
    declined_reason: str = ""


@dataclass(frozen=True)
class ActionEvent:
    event_id: str
    recommendation_id: str
    user_id: str
    module: str
    event_type: str
    occurred_at: datetime
    metadata: dict[str, Any]
    summary: str = ""
    request_fingerprint: str = ""
    compatible_fingerprints: tuple[str, ...] = field(
        default=(), repr=False, compare=False
    )
    request_summary: str = field(default="", repr=False, compare=False)


def _fingerprint_version(value: str) -> str:
    parts = value.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "hmac-sha256" else ""


def _event_core_matches(existing: ActionEvent, incoming: ActionEvent) -> bool:
    return (
        existing.event_id == incoming.event_id
        and existing.recommendation_id == incoming.recommendation_id
        and existing.user_id == incoming.user_id
        and existing.module == incoming.module
        and existing.event_type == incoming.event_type
        and existing.occurred_at == incoming.occurred_at
    )


def _is_compatible_replay(existing: ActionEvent, incoming: ActionEvent) -> bool:
    if existing.request_fingerprint == incoming.request_fingerprint:
        return True
    if existing.request_fingerprint in incoming.compatible_fingerprints:
        return _event_core_matches(existing, incoming)
    return (
        existing.request_fingerprint.startswith("legacy:")
        and _event_core_matches(existing, incoming)
    )


class MemoryActionRepository:
    def __init__(self, *, privacy_guard: Any | None = None):
        self._recommendations: dict[str, ActionRecommendation] = {}
        self._events: dict[str, ActionEvent] = {}
        self._feedback: dict[str, str] = {}
        self._lock = Lock()
        self.privacy_guard = privacy_guard

    def _write(self, user_id: str):
        return (
            self.privacy_guard.memory_write(user_id)
            if self.privacy_guard is not None
            else nullcontext()
        )

    def add_recommendation(self, recommendation: ActionRecommendation) -> ActionRecommendation:
        with self._write(recommendation.user_id), self._lock:
            self._recommendations[recommendation.recommendation_id] = recommendation
            return recommendation

    def get(self, recommendation_id: str) -> ActionRecommendation:
        with self._lock:
            try:
                return self._recommendations[recommendation_id]
            except KeyError as exc:
                raise ActionContractError("recommendation not found") from exc

    def find_declined(self, user_id: str, module: str, now: datetime, cooldown: timedelta) -> bool:
        with self._lock:
            return any(
                item.user_id == user_id and item.module == module and item.status == "declined"
                and item.created_at + cooldown > now
                for item in self._recommendations.values()
            )

    def has_recommended(self, user_id: str, session_id: str, module: str) -> bool:
        with self._lock:
            return any(
                item.user_id == user_id
                and item.session_id == session_id
                and item.module == module
                for item in self._recommendations.values()
            )

    def get_event(self, event_id: str) -> ActionEvent | None:
        with self._lock:
            return self._events.get(event_id)

    def apply_event(self, event: ActionEvent) -> tuple[ActionRecommendation, bool]:
        with self._write(event.user_id), self._lock:
            existing = self._events.get(event.event_id)
            if existing is not None:
                if not _is_compatible_replay(existing, event):
                    raise ActionEventConflict("action event fingerprint conflict")
                if existing.request_fingerprint != event.request_fingerprint:
                    self._events[event.event_id] = event
                return self._recommendations[existing.recommendation_id], False
            recommendation = self._recommendations.get(event.recommendation_id)
            if recommendation is None or recommendation.user_id != event.user_id or recommendation.module != event.module:
                raise ActionContractError("event does not match recommendation")
            allowed = {
                "recommended": {"accepted", "declined", "expired"},
                "accepted": {"completed", "declined"},
                "declined": set(),
                "completed": set(),
                "expired": set(),
            }
            if event.event_type not in allowed.get(recommendation.status, set()):
                raise ActionContractError("invalid action state transition")
            if (
                event.event_type == "expired"
                and (
                    recommendation.expires_at is None
                    or event.occurred_at < recommendation.expires_at
                )
            ):
                raise ActionContractError("recommendation is not expired")
            if (
                recommendation.expires_at
                and event.occurred_at > recommendation.expires_at
                and event.event_type != "expired"
            ):
                raise ActionContractError("recommendation expired")
            recommendation.status = event.event_type
            if event.event_type == "declined":
                reason = str(event.metadata.get("reason", ""))[:200]
                recommendation.declined_reason = reason
            self._events[event.event_id] = event
            if event.event_type == "completed":
                self._feedback[recommendation.recommendation_id] = event.summary
            return recommendation, True

    def set_feedback(self, recommendation_id: str, feedback: str) -> None:
        with self._lock:
            self._feedback[recommendation_id] = feedback

    def feedback(self, recommendation_id: str) -> str:
        with self._lock:
            return self._feedback.get(recommendation_id, "")

    def has_event(self, event_id: str) -> bool:
        return self.get_event(event_id) is not None

    def list_for_user(self, user_id: str) -> list[ActionRecommendation]:
        with self._lock:
            return [
                recommendation
                for recommendation in self._recommendations.values()
                if recommendation.user_id == user_id
            ]

    def delete_for_user(self, user_id: str) -> int:
        with self._lock:
            recommendation_ids = {
                recommendation.recommendation_id
                for recommendation in self._recommendations.values()
                if recommendation.user_id == user_id
            }
            for recommendation_id in recommendation_ids:
                self._recommendations.pop(recommendation_id, None)
                self._feedback.pop(recommendation_id, None)
            self._events = {
                event_id: event
                for event_id, event in self._events.items()
                if event.recommendation_id not in recommendation_ids
                and event.user_id != user_id
            }
            return len(recommendation_ids)


class PostgresActionRepository:
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
    def _recommendation(row) -> ActionRecommendation:
        return ActionRecommendation(*row)

    def add_recommendation(self, recommendation: ActionRecommendation) -> ActionRecommendation:
        import json

        with self._connect() as connection:
            self._protect(connection, recommendation.user_id)
            connection.execute(
                """
                INSERT INTO ai_users
                    (user_id, nickname, birth_year, status, created_at, updated_at)
                VALUES (%s, '', NULL, 'active', now(), now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (recommendation.user_id,),
            )
            row = connection.execute(
                """
                INSERT INTO ai_action_recommendations
                    (recommendation_id, user_id, session_id, module, action_json, reason, status,
                     source_message_id, expires_at, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING recommendation_id, user_id, session_id, module, action_json, reason,
                          status, source_message_id, expires_at, created_at, declined_reason
                """,
                (recommendation.recommendation_id, recommendation.user_id, recommendation.session_id,
                 recommendation.module, json.dumps(recommendation.action_json, ensure_ascii=False),
                 recommendation.reason, recommendation.status, recommendation.source_message_id,
                 recommendation.expires_at, recommendation.created_at),
            ).fetchone()
        return self._recommendation(row)

    def get(self, recommendation_id: str) -> ActionRecommendation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recommendation_id, user_id, session_id, module, action_json, reason, status, source_message_id, expires_at, created_at, declined_reason FROM ai_action_recommendations WHERE recommendation_id=%s",
                (recommendation_id,),
            ).fetchone()
        if not row:
            raise ActionContractError("recommendation not found")
        return self._recommendation(row)

    def find_declined(self, user_id: str, module: str, now: datetime, cooldown: timedelta) -> bool:
        with self._connect() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM ai_action_recommendations WHERE user_id=%s AND module=%s AND status='declined' AND created_at > %s LIMIT 1",
                (user_id, module, now - cooldown),
            ).fetchone())

    def has_recommended(self, user_id: str, session_id: str, module: str) -> bool:
        with self._connect() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM ai_action_recommendations WHERE user_id=%s AND session_id=%s AND module=%s LIMIT 1",
                (user_id, session_id, module),
            ).fetchone())

    def has_event(self, event_id: str) -> bool:
        return self.get_event(event_id) is not None

    def get_event(self, event_id: str) -> ActionEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_id, recommendation_id, user_id, module, event_type,
                       occurred_at, metadata, summary, request_fingerprint
                FROM ai_action_events WHERE event_id=%s
                """,
                (event_id,),
            ).fetchone()
        return ActionEvent(*row) if row else None

    def apply_event(self, event: ActionEvent) -> tuple[ActionRecommendation, bool]:
        with self._connect() as connection:
            self._protect(connection, event.user_id)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"action-event:{len(event.event_id)}:{event.event_id}",),
            )
            existing = connection.execute(
                """
                SELECT event_id, recommendation_id, user_id, module, event_type,
                       occurred_at, metadata, summary, request_fingerprint
                FROM ai_action_events WHERE event_id=%s
                """,
                (event.event_id,),
            ).fetchone()
            if existing:
                existing_event = ActionEvent(*existing)
                if not _is_compatible_replay(existing_event, event):
                    raise ActionEventConflict("action event fingerprint conflict")
                if existing_event.request_fingerprint != event.request_fingerprint:
                    connection.execute(
                        "UPDATE ai_action_events SET metadata=%s, summary=%s, request_fingerprint=%s WHERE event_id=%s",
                        (json.dumps(event.metadata), event.summary,
                         event.request_fingerprint, event.event_id),
                    )
                row = connection.execute(
                    "SELECT recommendation_id, user_id, session_id, module, action_json, reason, status, source_message_id, expires_at, created_at, declined_reason FROM ai_action_recommendations WHERE recommendation_id=%s",
                    (existing_event.recommendation_id,),
                ).fetchone()
                return self._recommendation(row), False
            row = connection.execute(
                "SELECT recommendation_id, user_id, session_id, module, action_json, reason, status, source_message_id, expires_at, created_at, declined_reason FROM ai_action_recommendations WHERE recommendation_id=%s FOR UPDATE",
                (event.recommendation_id,),
            ).fetchone()
            if not row:
                raise ActionContractError("recommendation not found")
            recommendation = self._recommendation(row)
            if recommendation.user_id != event.user_id or recommendation.module != event.module:
                raise ActionContractError("event does not match recommendation")
            allowed = {"recommended": {"accepted", "declined", "expired"}, "accepted": {"completed", "declined"}}
            if event.event_type not in allowed.get(recommendation.status, set()):
                raise ActionContractError("invalid action state transition")
            if (
                event.event_type == "expired"
                and (
                    recommendation.expires_at is None
                    or event.occurred_at < recommendation.expires_at
                )
            ):
                raise ActionContractError("recommendation is not expired")
            if (
                recommendation.expires_at
                and event.occurred_at > recommendation.expires_at
                and event.event_type != "expired"
            ):
                raise ActionContractError("recommendation expired")
            connection.execute(
                "INSERT INTO ai_action_events (event_id, recommendation_id, user_id, module, event_type, occurred_at, metadata, summary, request_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (event.event_id, event.recommendation_id, event.user_id, event.module,
                 event.event_type, event.occurred_at,
                 json.dumps(event.metadata, ensure_ascii=False), event.summary,
                 event.request_fingerprint),
            )
            declined_reason = str(event.metadata.get("reason", ""))[:200] if event.event_type == "declined" else recommendation.declined_reason
            row = connection.execute(
                "UPDATE ai_action_recommendations SET status=%s, declined_reason=%s, feedback=CASE WHEN %s='completed' THEN %s ELSE feedback END WHERE recommendation_id=%s RETURNING recommendation_id, user_id, session_id, module, action_json, reason, status, source_message_id, expires_at, created_at, declined_reason",
                (event.event_type, declined_reason, event.event_type, event.summary,
                 recommendation.recommendation_id),
            ).fetchone()
        return self._recommendation(row), True

    def set_feedback(self, recommendation_id: str, feedback: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE ai_action_recommendations SET feedback=%s WHERE recommendation_id=%s", (feedback, recommendation_id))

    def feedback(self, recommendation_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT feedback FROM ai_action_recommendations WHERE recommendation_id=%s", (recommendation_id,)).fetchone()
        return row[0] if row else ""


class ActionService:
    _MODULE_SUMMARIES = {
        "M1": "完成情绪签到",
        "M2": "完成互动游戏",
        "M3": "完成舒缓练习",
        "M5": "完成社区互动",
    }

    def __init__(
        self,
        repository: MemoryActionRepository,
        *,
        memory_service: MemoryService | None = None,
        decline_cooldown: timedelta = timedelta(hours=2),
        reference_service: HmacReferenceService | None = None,
    ):
        self.repository = repository
        self.memory_service = memory_service
        self.decline_cooldown = decline_cooldown
        self.reference_service = reference_service or HmacReferenceService(
            "xiaoliao-development-action-key"
        )

    def recommend(
        self,
        user_id: str,
        session_id: str,
        action: dict[str, Any],
        *,
        source_message_id: str,
        deduplicate: bool = True,
    ) -> ActionRecommendation:
        try:
            payload = ActionPayload.model_validate(action)
        except Exception as exc:
            raise ActionContractError("action_policy_violation") from exc
        now = datetime.now(timezone.utc)
        if deduplicate:
            if self.repository.has_recommended(user_id, session_id, payload.module):
                raise ActionAlreadyRecommended("action already recommended in this session")
            if self.repository.find_declined(user_id, payload.module, now, self.decline_cooldown):
                raise ActionContractError("action recommendation cooldown")
        expires_at = None
        if payload.expires_at:
            expires_at = datetime.fromisoformat(payload.expires_at.replace("Z", "+00:00"))
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ActionContractError("action expiry must include timezone")
            if expires_at <= now:
                raise ActionContractError("action is expired")
        recommendation = ActionRecommendation(
            recommendation_id=uuid.uuid4().hex,
            user_id=user_id,
            session_id=session_id,
            module=payload.module,
            action_json=payload.model_dump(),
            reason=payload.reason,
            status="recommended",
            source_message_id=source_message_id,
            expires_at=expires_at,
            created_at=now,
        )
        return self.repository.add_recommendation(recommendation)

    def record_event(self, event: ActionEvent) -> ActionRecommendation:
        recommendation, _created = self.record_event_with_status(event)
        return recommendation

    def record_event_with_status(
        self,
        event: ActionEvent,
    ) -> tuple[ActionRecommendation, bool]:
        if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
            raise ActionContractError("action event time must include timezone")
        fingerprint_metadata = {
            str(key): value
            for key, value in event.metadata.items()
            if value not in (None, "")
        }
        summary = (
            self._MODULE_SUMMARIES[event.module]
            if event.event_type == "completed"
            else ""
        )
        canonical = json.dumps(
            {
                "event_id": event.event_id,
                "recommendation_id": event.recommendation_id,
                "user_id": event.user_id,
                "module": event.module,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "summary": event.request_summary[:200],
                "metadata": fingerprint_metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint_candidates = self.reference_service.fingerprint_candidates(
            "action-event", canonical
        )
        normalized = replace(
            event,
            metadata={},
            summary=summary,
            request_fingerprint=fingerprint_candidates[0],
            compatible_fingerprints=fingerprint_candidates,
        )
        recommendation, created = self.repository.apply_event(normalized)
        if normalized.event_type == "completed":
            if self.memory_service is not None:
                try:
                    self.memory_service.save_versioned_candidate(
                        recommendation.user_id,
                        MemoryCandidate(
                            memory_type="action_summary",
                            content=summary,
                            confidence=1.0,
                            source_message_id=normalized.event_id,
                            explicitly_stated=True,
                            sensitive=normalized.module in {"M1", "M3"},
                            memory_key=(
                                "action:" + recommendation.recommendation_id
                            ),
                            source_type="action_event",
                        ),
                    )
                except (
                    ConsentRequiredError,
                    SensitiveConsentRequiredError,
                    SubjectDeletedError,
                ):
                    pass
                except Exception as exc:
                    raise ActionMemoryUnavailable(
                        "action memory storage is unavailable"
                    ) from exc
        return recommendation, created

    def feedback_for(self, recommendation_id: str) -> str:
        return self.repository.feedback(recommendation_id)


from datetime import datetime
from typing import Any



ALLOWED_ACTION_MODULES = frozenset({"M1", "M2", "M3", "M5"})


class UnknownActionModuleError(ValueError):
    pass


def handle_action_event(service: ActionService, event: dict[str, Any]) -> dict[str, Any]:
    module = event.get("module")
    if module not in ALLOWED_ACTION_MODULES:
        raise UnknownActionModuleError(f"unknown action module: {module}")
    if event.get("event_type") != "completed":
        raise ValueError("wecom action event must be completed")
    occurred = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
    action_event = ActionEvent(
        event_id=str(event["event_id"]),
        recommendation_id=str(event["recommendation_id"]),
        user_id=str(event["user_id"]),
        module=str(module),
        event_type="completed",
        occurred_at=occurred,
        metadata=dict(event.get("metadata") or {}),
    )
    recommendation, created = service.record_event_with_status(action_event)
    return {
        "status": "ok",
        "recommendation_id": recommendation.recommendation_id,
        "duplicate": not created,
    }
