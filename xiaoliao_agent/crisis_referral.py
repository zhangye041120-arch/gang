from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json

from .crisis_repository import new_crisis_event
from .notifications import CrisisNotifier


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
