from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Protocol
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
    def __init__(self, *, privacy_guard: Any | None = None):
        self._events: dict[str, CrisisEvent] = {}
        self.privacy_guard = privacy_guard

    def add(self, event: CrisisEvent) -> None:
        guard = (
            self.privacy_guard.memory_write(event.user_id)
            if self.privacy_guard is not None
            else nullcontext()
        )
        with guard:
            self._events.setdefault(event.event_id, event)

    def list_events(self) -> list[CrisisEvent]:
        return list(self._events.values())

    def delete_for_user(self, user_id: str) -> int:
        event_ids = [
            event_id for event_id, event in self._events.items()
            if event.user_id == user_id
        ]
        for event_id in event_ids:
            self._events.pop(event_id, None)
        return len(event_ids)


class PostgresCrisisEventRepository:
    def __init__(self, database_url: str, *, privacy_guard: Any | None = None):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - integration dependency
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)
        self.privacy_guard = privacy_guard

    def add(self, event: CrisisEvent) -> None:  # pragma: no cover - covered by integration test
        with self._connect() as connection:
            if self.privacy_guard is not None:
                self.privacy_guard.protect_postgres_write(connection, event.user_id)
            connection.execute(
                """
                INSERT INTO ai_users
                    (user_id, nickname, birth_year, status, created_at, updated_at)
                VALUES (%s, '', NULL, 'active', now(), now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (event.user_id,),
            )
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


from collections.abc import Callable



class CrisisNotifier:
    def __init__(
        self,
        *,
        route: str,
        sender: Callable[[CrisisEvent], None] | None = None,
        max_attempts: int = 2,
    ):
        self.route = route.strip() or "unconfigured"
        self.sender = sender
        self.max_attempts = max(1, max_attempts)
        self._processed: set[str] = set()

    def notify(self, event: CrisisEvent) -> list[str]:
        if event.event_id in self._processed:
            return []
        if self.route == "unconfigured" and self.sender is None:
            self._processed.add(event.event_id)
            return ["route_unconfigured"]
        if self.sender is None:
            self._processed.add(event.event_id)
            return ["notification_sender_unconfigured"]
        for _ in range(self.max_attempts):
            try:
                self.sender(event)
                self._processed.add(event.event_id)
                return []
            except Exception:
                continue
        self._processed.add(event.event_id)
        return ["notification_failed"]


from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json



@dataclass(frozen=True)
class CrisisReferralRoute:
    region: str
    hotline: str | None = None
    on_call: str | None = None
    authorization_required: bool = True


@dataclass(frozen=True)
class CrisisReferralConfig:
    routes: dict[str, CrisisReferralRoute] = field(default_factory=dict)
    default_region: str = ""


def load_crisis_referral_config(path: str | Path) -> CrisisReferralConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    routes = {
        region: CrisisReferralRoute(
            region=str(item.get("region", region)),
            hotline=item.get("hotline"),
            on_call=item.get("on_call"),
            authorization_required=bool(item.get("authorization_required", True)),
        )
        for region, item in data.get("routes", {}).items()
    }
    return CrisisReferralConfig(routes=routes, default_region=str(data.get("default_region", "")))


class CrisisReferralService:
    def __init__(
        self,
        config: CrisisReferralConfig,
        *,
        notifier: CrisisNotifier | None = None,
        repository=None,
    ):
        self.config = config
        self.notifier = notifier or CrisisNotifier(route="unconfigured")
        self.repository = repository
        self._processed: set[str] = set()

    def refer(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(payload.get("event_id", "")).strip()
        if not event_id:
            raise ValueError("crisis referral requires event_id")
        if event_id in self._processed:
            return {"status": "ok", "alerts": [], "duplicate": True}
        region = str(payload.get("region") or self.config.default_region)
        route = self.config.routes.get(region)
        alerts: list[str] = []
        hotline = None
        on_call = None
        if route is None:
            alerts.append("crisis_region_unconfigured")
        else:
            hotline = route.hotline
            on_call = route.on_call
            if route.authorization_required and not payload.get("authorized"):
                alerts.append("crisis_authorization_missing")
            if not hotline and not on_call:
                alerts.append("crisis_contact_missing")
            else:
                event = new_crisis_event(
                    user_id=str(payload.get("user_id", "")),
                    session_id=str(payload.get("session_id", "")),
                    evidence_code=str(payload.get("evidence_code", "C_WECOM")),
                    route=region,
                    prompt_version=str(payload.get("prompt_version", "integration-placeholder")),
                    response_version=str(payload.get("response_version", "wecom-v1.0.0")),
                )
                event.event_id = event_id
                if self.repository is not None:
                    self.repository.add(event)
                alerts.extend(self.notifier.notify(event))
        self._processed.add(event_id)
        return {
            "status": "alert" if alerts else "ok",
            "alerts": alerts,
            "hotline": hotline,
            "on_call": on_call,
            "duplicate": False,
        }
