from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .migrations import verify_schema


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, bool]


class RuntimeReadiness:
    def __init__(
        self,
        settings: Settings,
        agent_provider: Callable[[], Any],
        runtime_state: Any | None = None,
    ):
        self.settings = settings
        self.agent_provider = agent_provider
        self.runtime_state = runtime_state

    async def check(self) -> ReadinessResult:
        checks: dict[str, bool] = {}
        if self.settings.knowledge_database_url:
            try:
                await asyncio.to_thread(
                    verify_schema,
                    self.settings.knowledge_database_url,
                )
                checks["postgres"] = True
                checks["schema"] = True
            except Exception:
                checks["postgres"] = False
                checks["schema"] = False
        else:
            checks["postgres"] = not self.settings.is_production
            checks["schema"] = not self.settings.is_production

        if self.runtime_state is not None:
            try:
                checks["redis"] = bool(await self.runtime_state.ping())
            except Exception:
                checks["redis"] = False
        else:
            checks["redis"] = not self.settings.is_production

        try:
            agent = self.agent_provider()
            checks["knowledge"] = bool(getattr(getattr(agent, "kb", None), "chunks", []))
        except Exception:
            checks["knowledge"] = False
        checks["crisis_route"] = (
            not self.settings.is_production
            or self.settings.crisis_route.strip().lower() != "unconfigured"
        )
        return ReadinessResult(all(checks.values()), checks)
