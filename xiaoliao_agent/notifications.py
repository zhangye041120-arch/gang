from collections.abc import Callable

from .crisis_repository import CrisisEvent


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
        if self.route == "unconfigured":
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
