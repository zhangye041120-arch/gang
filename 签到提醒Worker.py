import hashlib
import json
import logging
import time

import redis

from xiaoliao_agent.config import Settings
from xiaoliao_agent.observability import configure_logging
from xiaoliao_agent.reminders import DailyCheckinService, checkin_policy_from_settings


logger = logging.getLogger("xiaoliao.签到提醒Worker")


class RedisCheckinRepository:
    def __init__(self, redis_url: str, *, prefix: str, retention_days: int = 8):
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.prefix = prefix
        self.ttl_seconds = retention_days * 86_400

    def _key(self, target: str, day: str, slot: str) -> str:
        digest = hashlib.sha256(f"{target}\n{day}\n{slot}".encode()).hexdigest()
        return f"{self.prefix}:checkin-send:{digest}"

    def is_sent(self, target: str, day: str, slot: str) -> bool:
        return bool(self.client.exists(self._key(target, day, slot)))

    def mark_sent(self, target: str, day: str, slot: str, sent_at=None) -> None:
        self.client.set(
            self._key(target, day, slot),
            "1",
            nx=True,
            ex=self.ttl_seconds,
        )


def main() -> int:
    settings = Settings.from_env()
    configure_logging(settings.app_env)
    if not settings.wecom_checkin_reminder_enabled:
        raise RuntimeError("WECOM_CHECKIN_REMINDER_ENABLED must be true")
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required")
    policy = checkin_policy_from_settings(settings)
    if not policy.user_schedules and not policy.webhook_url:
        raise RuntimeError("check-in webhook or user schedules are required")
    if policy.user_schedules and not (
        policy.corp_id and policy.agent_id and policy.agent_secret
    ):
        raise RuntimeError("WeCom app credentials are required for user schedules")
    service = DailyCheckinService(
        policy,
        repository=RedisCheckinRepository(
            settings.redis_url,
            prefix=settings.redis_key_prefix,
        ),
    )
    while True:
        try:
            results = service.run_due()
            counts: dict[str, int] = {}
            for result in results:
                status = str(result.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
            logger.info(json.dumps({"event": "checkin_cycle", "counts": counts}))
        except Exception:
            logger.error(json.dumps({
                "event": "checkin_cycle_failed",
                "error_code": "CHECKIN_DEPENDENCY_UNAVAILABLE",
            }))
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
