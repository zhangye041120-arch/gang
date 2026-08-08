"""Daily check-in reminder scheduling for an Enterprise WeChat employee group.

The reminder is sent through an internal group robot webhook.  There is no
customer-service 48-hour window for internal employee groups, so a configured
time slot can fire every day.  Sending is idempotent per (date, slot) and a
failed send is not marked as done, so the next scheduler tick may retry it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .wecom_outbound import WeComAppMessageSender, WeComWebhookSender


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CHECKIN_MESSAGE = (
    "小辽提醒：方便的时候花一分钟完成今天的情绪签到吧，"
    "记下此刻的心情就好，不用着急。"
)
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_csv(raw: str | None) -> list[str]:
    return [
        item.strip()
        for item in (raw or "").split(",")
        if item.strip()
    ]


def parse_schedule_times(raw: str | None) -> tuple[str, ...]:
    slots: list[str] = []
    for part in re.split(r"[,，;；\s]+", (raw or "").strip()):
        part = part.strip()
        if not part:
            continue
        if not _TIME_PATTERN.fullmatch(part):
            raise ValueError(f"invalid check-in time slot: {part!r}")
        slots.append(part)
    return tuple(sorted(set(slots)))


@dataclass(frozen=True)
class CheckinUserSchedule:
    user_id: str = ""
    times: tuple[str, ...] = ()

    @property
    def target_key(self) -> str:
        return f"user:{self.user_id}"


def load_checkin_user_schedules(path: str | Path) -> tuple[CheckinUserSchedule, ...]:
    """Load per-user reminder times from a JSON file."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"check-in user schedule file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid check-in user schedule JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
        raise ValueError("check-in user schedule must be an object with a users list")

    schedules: list[CheckinUserSchedule] = []
    for index, item in enumerate(raw["users"]):
        if not isinstance(item, dict):
            raise ValueError(f"check-in user schedule item {index} must be an object")
        user_id = str(item.get("user_id", "")).strip()
        if not user_id:
            raise ValueError(f"check-in user schedule item {index} needs user_id")
        raw_times = item.get("times", [])
        if isinstance(raw_times, str):
            times = parse_schedule_times(raw_times)
        elif isinstance(raw_times, list):
            times = parse_schedule_times(",".join(str(value) for value in raw_times))
        else:
            raise ValueError(
                f"check-in user schedule item {index} times must be a list or string"
            )
        if not times:
            raise ValueError(
                f"check-in user schedule item {index} needs at least one time"
            )
        schedules.append(CheckinUserSchedule(
            user_id=user_id,
            times=times,
        ))
    return tuple(schedules)


@dataclass(frozen=True)
class CheckinReminderPolicy:
    enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""
    corp_id: str = ""
    agent_id: str = ""
    agent_secret: str = ""
    times: tuple[str, ...] = ("09:00",)
    user_schedules: tuple[CheckinUserSchedule, ...] = ()
    message: str = DEFAULT_CHECKIN_MESSAGE
    mention_all: bool = False
    mentioned_user_ids: tuple[str, ...] = ()
    mentioned_mobiles: tuple[str, ...] = ()
    catch_up_minutes: int = 15
    timezone_name: str = "Asia/Shanghai"
    state_path: str = ""


def checkin_policy_from_settings(settings: Any) -> CheckinReminderPolicy:
    schedule_path = str(settings.wecom_checkin_group_user_schedule_path or "").strip()
    return CheckinReminderPolicy(
        enabled=bool(settings.wecom_checkin_reminder_enabled),
        webhook_url=str(settings.wecom_checkin_group_webhook or "").strip(),
        webhook_secret=str(settings.wecom_checkin_group_webhook_secret or "").strip(),
        corp_id=str(settings.wecom_corp_id or "").strip(),
        agent_id=str(settings.wecom_agent_id or "").strip(),
        agent_secret=str(settings.wecom_agent_secret or "").strip(),
        times=parse_schedule_times(settings.wecom_checkin_group_times),
        user_schedules=(
            load_checkin_user_schedules(schedule_path)
            if schedule_path
            else ()
        ),
        message=str(settings.wecom_checkin_group_message or "").strip()
        or DEFAULT_CHECKIN_MESSAGE,
        mention_all=bool(settings.wecom_checkin_group_mention_all),
        mentioned_user_ids=tuple(parse_csv(settings.wecom_checkin_group_mentioned_users)),
        mentioned_mobiles=tuple(parse_csv(settings.wecom_checkin_group_mentioned_mobiles)),
        catch_up_minutes=max(1, int(settings.wecom_checkin_group_catch_up_minutes)),
        state_path=str(settings.wecom_checkin_group_state_path or "").strip(),
    )


class MemoryCheckinReminderRepository:
    def __init__(self) -> None:
        self._sent: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def is_sent(self, target: str, day: str, slot: str) -> bool:
        with self._lock:
            return (target, day, slot) in self._sent

    def mark_sent(
        self,
        target: str,
        day: str,
        slot: str,
        sent_at: datetime | None = None,
    ) -> None:
        stamp = sent_at or datetime.now(timezone.utc)
        with self._lock:
            self._sent[(target, day, slot)] = stamp.isoformat()


class SqliteCheckinReminderRepository:
    """Durable idempotency store so restarts do not resend the same slot."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_reminder_sends (
                target TEXT NOT NULL,
                send_date TEXT NOT NULL,
                slot TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (target, send_date, slot)
            )
            """
        )
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO checkin_reminder_sends
                    (target, send_date, slot, sent_at)
                SELECT 'group', send_date, slot, sent_at
                FROM checkin_reminders
                """
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def is_sent(self, target: str, day: str, slot: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM checkin_reminder_sends
                WHERE target = ? AND send_date = ? AND slot = ?
                """,
                (target, day, slot),
            ).fetchone()
        return row is not None

    def mark_sent(
        self,
        target: str,
        day: str,
        slot: str,
        sent_at: datetime | None = None,
    ) -> None:
        stamp = (sent_at or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO checkin_reminder_sends
                    (target, send_date, slot, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (target, day, slot, stamp),
            )
            self._conn.commit()


def _build_repository(policy: CheckinReminderPolicy):
    if policy.state_path:
        return SqliteCheckinReminderRepository(policy.state_path)
    return MemoryCheckinReminderRepository()


def _slot_due(current: datetime, slot: str, catch_up_minutes: int) -> bool:
    hour_text, minute_text = slot.split(":")
    slot_time = current.replace(
        hour=int(hour_text),
        minute=int(minute_text),
        second=0,
        microsecond=0,
    )
    delta_seconds = (current - slot_time).total_seconds()
    return 0 <= delta_seconds < max(1, catch_up_minutes) * 60


class DailyCheckinService:
    def __init__(
        self,
        policy: CheckinReminderPolicy,
        *,
        repository=None,
        sender=None,
    ) -> None:
        self.policy = policy
        self.repository = repository or _build_repository(policy)
        self._sender = sender
        self._default_sender: WeComWebhookSender | None = None
        self._default_app_sender: WeComAppMessageSender | None = None

    def _get_sender(self) -> WeComWebhookSender | None:
        if self._sender is not None:
            return self._sender
        if not self.policy.webhook_url:
            return None
        if self._default_sender is None:
            self._default_sender = WeComWebhookSender(
                self.policy.webhook_url,
                secret=self.policy.webhook_secret,
            )
        return self._default_sender

    def _get_app_sender(self) -> WeComAppMessageSender | None:
        if self._sender is not None:
            return self._sender
        if not self.policy.corp_id or not self.policy.agent_id or not self.policy.agent_secret:
            return None
        if self._default_app_sender is None:
            self._default_app_sender = WeComAppMessageSender(
                self.policy.corp_id,
                self.policy.agent_id,
                self.policy.agent_secret,
            )
        return self._default_app_sender

    def run_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(BEIJING_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TZ)
        else:
            current = current.astimezone(BEIJING_TZ)
        if not self.policy.enabled:
            return [{"status": "blocked", "reason": "global_disabled"}]
        if not self.policy.times and not self.policy.user_schedules:
            return [{"status": "blocked", "reason": "no_times_configured"}]

        results: list[dict[str, Any]] = []
        day = current.date().isoformat()
        if self.policy.user_schedules:
            app_sender = self._get_app_sender()
            if app_sender is None:
                return [{"status": "blocked", "reason": "app_message_not_configured"}]
            for schedule in self.policy.user_schedules:
                for slot in schedule.times:
                    if not _slot_due(current, slot, self.policy.catch_up_minutes):
                        continue
                    target = schedule.target_key
                    event = {
                        "target": target,
                        "user_id": schedule.user_id,
                        "day": day,
                        "slot": slot,
                    }
                    if self.repository.is_sent(target, day, slot):
                        results.append({"status": "duplicate", **event})
                        continue
                    try:
                        app_sender.send_text(schedule.user_id, self.policy.message)
                    except Exception as exc:
                        results.append({
                            "status": "failed",
                            **event,
                            "error": str(exc)[:500],
                        })
                        continue
                    self.repository.mark_sent(target, day, slot, sent_at=current)
                    results.append({"status": "sent", **event})
            return results

        sender = self._get_sender()
        if sender is None:
            return [{"status": "blocked", "reason": "webhook_not_configured"}]

        target = "group"
        for slot in self.policy.times:
            if not _slot_due(current, slot, self.policy.catch_up_minutes):
                continue
            if self.repository.is_sent(target, day, slot):
                results.append({
                    "status": "duplicate",
                    "target": target,
                    "day": day,
                    "slot": slot,
                })
                continue
            try:
                sender.send_text(
                    self.policy.message,
                    mentioned_user_ids=self.policy.mentioned_user_ids,
                    mentioned_mobiles=self.policy.mentioned_mobiles,
                    mention_all=self.policy.mention_all,
                )
            except Exception as exc:
                results.append({
                    "status": "failed",
                    "target": target,
                    "day": day,
                    "slot": slot,
                    "error": str(exc)[:500],
                })
                continue
            self.repository.mark_sent(target, day, slot, sent_at=current)
            results.append({
                "status": "sent",
                "target": target,
                "day": day,
                "slot": slot,
            })
        return results
