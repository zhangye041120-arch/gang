from datetime import datetime
from typing import Any

from .actions import ActionEvent, ActionService


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
    recommendation = service.record_event(action_event)
    return {
        "status": "ok",
        "recommendation_id": recommendation.recommendation_id,
        "duplicate": service.repository.has_event(action_event.event_id),
    }
