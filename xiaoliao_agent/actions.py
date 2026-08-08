from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
import uuid

from .memory import MemoryCandidate, MemoryService
from .schemas import ActionPayload


class ActionContractError(ValueError):
    pass


class ActionAlreadyRecommended(ActionContractError):
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


class MemoryActionRepository:
    def __init__(self):
        self._recommendations: dict[str, ActionRecommendation] = {}
        self._events: dict[str, ActionEvent] = {}
        self._feedback: dict[str, str] = {}
        self._lock = Lock()

    def add_recommendation(self, recommendation: ActionRecommendation) -> ActionRecommendation:
        with self._lock:
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

    def apply_event(self, event: ActionEvent) -> ActionRecommendation:
        with self._lock:
            if event.event_id in self._events:
                return self._recommendations[event.recommendation_id]
            recommendation = self._recommendations.get(event.recommendation_id)
            if recommendation is None or recommendation.user_id != event.user_id or recommendation.module != event.module:
                raise ActionContractError("event does not match recommendation")
            now = event.occurred_at
            if recommendation.expires_at and now > recommendation.expires_at:
                recommendation.status = "expired"
                raise ActionContractError("recommendation expired")
            allowed = {
                "recommended": {"accepted", "declined", "expired"},
                "accepted": {"completed", "declined"},
                "declined": set(),
                "completed": set(),
                "expired": set(),
            }
            if event.event_type not in allowed.get(recommendation.status, set()):
                raise ActionContractError("invalid action state transition")
            recommendation.status = event.event_type
            if event.event_type == "declined":
                reason = str(event.metadata.get("reason", ""))[:200]
                recommendation.declined_reason = reason
            self._events[event.event_id] = event
            return recommendation

    def set_feedback(self, recommendation_id: str, feedback: str) -> None:
        with self._lock:
            self._feedback[recommendation_id] = feedback

    def feedback(self, recommendation_id: str) -> str:
        with self._lock:
            return self._feedback.get(recommendation_id, "")

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events


class PostgresActionRepository:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)

    @staticmethod
    def _recommendation(row) -> ActionRecommendation:
        return ActionRecommendation(*row)

    def add_recommendation(self, recommendation: ActionRecommendation) -> ActionRecommendation:
        import json

        with self._connect() as connection:
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
        with self._connect() as connection:
            return bool(connection.execute("SELECT 1 FROM ai_action_events WHERE event_id=%s", (event_id,)).fetchone())

    def apply_event(self, event: ActionEvent) -> ActionRecommendation:
        import json

        with self._connect() as connection:
            if self.has_event(event.event_id):
                return self.get(event.recommendation_id)
            recommendation = self.get(event.recommendation_id)
            if recommendation.user_id != event.user_id or recommendation.module != event.module:
                raise ActionContractError("event does not match recommendation")
            if recommendation.expires_at and event.occurred_at > recommendation.expires_at:
                raise ActionContractError("recommendation expired")
            allowed = {"recommended": {"accepted", "declined", "expired"}, "accepted": {"completed", "declined"}}
            if event.event_type not in allowed.get(recommendation.status, set()):
                raise ActionContractError("invalid action state transition")
            connection.execute(
                "INSERT INTO ai_action_events (event_id, recommendation_id, user_id, module, event_type, occurred_at, metadata) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (event.event_id, event.recommendation_id, event.user_id, event.module, event.event_type, event.occurred_at, json.dumps(event.metadata, ensure_ascii=False)),
            )
            declined_reason = str(event.metadata.get("reason", ""))[:200] if event.event_type == "declined" else recommendation.declined_reason
            row = connection.execute(
                "UPDATE ai_action_recommendations SET status=%s, declined_reason=%s WHERE recommendation_id=%s RETURNING recommendation_id, user_id, session_id, module, action_json, reason, status, source_message_id, expires_at, created_at, declined_reason",
                (event.event_type, declined_reason, recommendation.recommendation_id),
            ).fetchone()
        return self._recommendation(row)

    def set_feedback(self, recommendation_id: str, feedback: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE ai_action_recommendations SET feedback=%s WHERE recommendation_id=%s", (feedback, recommendation_id))

    def feedback(self, recommendation_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT feedback FROM ai_action_recommendations WHERE recommendation_id=%s", (recommendation_id,)).fetchone()
        return row[0] if row else ""


class ActionService:
    def __init__(
        self,
        repository: MemoryActionRepository,
        *,
        memory_service: MemoryService | None = None,
        decline_cooldown: timedelta = timedelta(hours=2),
    ):
        self.repository = repository
        self.memory_service = memory_service
        self.decline_cooldown = decline_cooldown

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
        if self.repository.has_event(event.event_id):
            return self.repository.get(event.recommendation_id)
        recommendation = self.repository.apply_event(event)
        if event.event_type == "completed":
            activity = str(event.metadata.get("activity", recommendation.module))[:100]
            effort = str(event.metadata.get("effort", "完成了这一步"))[:200]
            feedback = f"你实际完成了{activity}，这次具体做的是：{effort}。下次可以根据自己的状态选择是否继续。"
            self.repository.set_feedback(recommendation.recommendation_id, feedback)
            if self.memory_service is not None:
                try:
                    self.memory_service.save_candidate(
                        recommendation.user_id,
                        MemoryCandidate(
                            memory_type="action_summary",
                            content=f"完成{activity}：{effort}",
                            confidence=1.0,
                            source_message_id=event.event_id,
                            explicitly_stated=True,
                        ),
                    )
                except Exception:
                    pass
        return recommendation

    def feedback_for(self, recommendation_id: str) -> str:
        return self.repository.feedback(recommendation_id)
