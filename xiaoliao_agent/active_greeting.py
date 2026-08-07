from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class GreetingPolicy:
    enabled: bool = False
    require_personalization: bool = True
    require_active_message_authorization: bool = True
    dnd_periods: tuple[tuple[int, int], ...] = ()
    max_per_day: int = 1
    recent_active_hours: float = 24.0
    emotion_trend_low_threshold: float = 0.4


def greeting_policy_from_settings(settings: Any) -> GreetingPolicy:
    return GreetingPolicy(
        enabled=bool(settings.wecom_active_greeting_enabled),
        max_per_day=int(settings.wecom_active_greeting_max_per_day),
        recent_active_hours=float(settings.wecom_active_greeting_recent_active_hours),
    )


@dataclass(frozen=True)
class GreetingState:
    user_id: str
    personalization: bool = True
    active_message_authorized: bool = True
    user_disabled: bool = False
    dnd: bool = False
    last_active_at: datetime | None = None
    emotion_trend: float = 0.5
    sent_today: int = 0


def _in_dnd(periods: tuple[tuple[int, int], ...], hour: int) -> bool:
    for start, end in periods:
        if start <= end:
            if start <= hour < end:
                return True
        elif hour >= start or hour < end:
            return True
    return False


def greeting_allowed(
    policy: GreetingPolicy,
    state: GreetingState,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    current = now or datetime.now(timezone.utc)
    if not policy.enabled:
        return False, "global_disabled"
    if state.user_disabled:
        return False, "user_disabled"
    if policy.require_personalization and not state.personalization:
        return False, "personalization_required"
    if policy.require_active_message_authorization and not state.active_message_authorized:
        return False, "active_message_authorization_required"
    if state.dnd or _in_dnd(policy.dnd_periods, current.hour):
        return False, "dnd"
    if state.last_active_at is not None:
        active_seconds = policy.recent_active_hours * 3600
        if (current - state.last_active_at).total_seconds() < active_seconds:
            return False, "recent_active"
    if state.sent_today >= max(1, policy.max_per_day):
        return False, "daily_limit"
    if state.emotion_trend < policy.emotion_trend_low_threshold:
        return False, "emotion_trend_low"
    return True, "ok"


class GreetingService:
    def __init__(
        self,
        policy: GreetingPolicy,
        *,
        state_provider=None,
        greeting_text: str = "小辽来看看你今天心情怎么样，想不想聊几句？",
    ):
        self.policy = policy
        self.state_provider = state_provider
        self.greeting_text = greeting_text
        self._sent: dict[str, dict[str, str]] = {}
        self._lock = Lock()

    def try_send(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        state: GreetingState | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if state is None:
            state = (
                self.state_provider(user_id)
                if self.state_provider
                else GreetingState(user_id=user_id)
            )
        allowed, reason = greeting_allowed(self.policy, state, now=current)
        if not allowed:
            return {"status": "blocked", "reason": reason}
        day_key = current.date().isoformat()
        with self._lock:
            sent = self._sent.setdefault(user_id, {})
            if day_key in sent:
                return {"status": "duplicate"}
            sent[day_key] = current.isoformat()
        return {"status": "ok", "greeting": self.greeting_text}
