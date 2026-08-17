"""Local reminder parsing and storage for the companion agent.

The reminder tool only records what the user asked for; it never modifies the
system clock, schedules OS alarms, or pushes messages.  Persistence is
in-memory by default; production delivery still requires Java/WeCom push plus
an external durable store.
"""


from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from contextlib import nullcontext
import re
import threading
import uuid
from typing import Any, Protocol
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")

_REMINDER_TRIGGERS = re.compile(r"(提醒|别忘了|记一下|设个提醒|到点|闹钟)", re.I)
_TOPICS = re.compile(r"(吃药|买菜|复诊|喝水|锻炼|散步|量血压|接孩子|做饭|睡觉|关煤气|缴(?:水|电|燃)?费|取药)")
_TIME_PATTERN = re.compile(
    r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)?\s*(\d{1,2})(?::(\d{2}))?\s*点(?:\s*(\d{1,2})分)?"
)
_MINUTES_FROM_NOW = re.compile(r"(\d{1,3})\s*分钟后")
_DAY_MARKERS = {
    "今天": 0,
    "今晚": 0,
    "明天": 1,
    "明早": 1,
    "明晚": 1,
    "后天": 2,
}



@dataclass(frozen=True)
class ReminderRequest:
    content: str
    due_at: datetime
    recurring: str | None = None

    def fingerprint(self) -> str:
        canonical = (
            f"{self.content}|{self.due_at.isoformat()}|{self.recurring or ''}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    user_id: str
    content: str
    due_at: datetime
    recurring: str | None
    created_at: datetime
    fingerprint: str
    source_request_id: str = ""


class ReminderRepository(Protocol):
    def list_for_user(self, user_id: str) -> list[Reminder]: ...

    def create_if_absent(
        self,
        reminder: Reminder,
        *,
        since: datetime,
    ) -> tuple[Reminder, bool]: ...

    def delete_for_user(self, user_id: str) -> int: ...


class MemoryReminderRepository:
    def __init__(self, *, privacy_guard: Any | None = None) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, list[Reminder]] = {}
        self.privacy_guard = privacy_guard

    def _write(self, user_id: str):
        return (
            self.privacy_guard.memory_write(user_id)
            if self.privacy_guard is not None
            else nullcontext()
        )

    def list_for_user(self, user_id: str) -> list[Reminder]:
        with self._lock:
            return list(self._items.get(user_id, []))

    def add(self, reminder: Reminder) -> Reminder:
        with self._write(reminder.user_id), self._lock:
            self._items.setdefault(reminder.user_id, []).append(reminder)
        return reminder

    def create_if_absent(
        self,
        reminder: Reminder,
        *,
        since: datetime,
    ) -> tuple[Reminder, bool]:
        with self._write(reminder.user_id), self._lock:
            existing = next(
                (
                    item
                    for item in self._items.get(reminder.user_id, [])
                    if item.fingerprint == reminder.fingerprint
                ),
                None,
            )
            if existing is not None:
                return existing, False
            self._items.setdefault(reminder.user_id, []).append(reminder)
            return reminder, True

    def delete_for_user(self, user_id: str) -> int:
        with self._lock:
            return len(self._items.pop(user_id, []))


class PostgresReminderRepository:
    def __init__(self, database_url: str, *, privacy_guard: Any | None = None):
        if not database_url:
            raise ValueError("database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)
        self.privacy_guard = privacy_guard

    @staticmethod
    def _from_row(row) -> Reminder:
        return Reminder(*row)

    def list_for_user(self, user_id: str) -> list[Reminder]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reminder_id, user_id, content, due_at, recurring,
                       created_at, fingerprint, source_request_id
                FROM ai_reminders
                WHERE user_id=%s AND status='active'
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def create_if_absent(
        self,
        reminder: Reminder,
        *,
        since: datetime,
    ) -> tuple[Reminder, bool]:
        lock_key = f"reminder:{reminder.user_id}:{reminder.fingerprint}"
        with self._connect() as connection:
            if self.privacy_guard is not None:
                self.privacy_guard.protect_postgres_write(
                    connection, reminder.user_id
                )
            connection.execute(
                """
                INSERT INTO ai_users
                    (user_id, nickname, birth_year, status, created_at, updated_at)
                VALUES (%s, '', NULL, 'active', now(), now())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (reminder.user_id,),
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            row = connection.execute(
                """
                SELECT reminder_id, user_id, content, due_at, recurring,
                       created_at, fingerprint, source_request_id
                FROM ai_reminders
                WHERE user_id=%s AND fingerprint=%s
                LIMIT 1
                """,
                (reminder.user_id, reminder.fingerprint),
            ).fetchone()
            if row is not None:
                return self._from_row(row), False
            row = connection.execute(
                """
                INSERT INTO ai_reminders
                    (reminder_id, user_id, content, due_at, recurring, created_at,
                     fingerprint, source_request_id, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')
                RETURNING reminder_id, user_id, content, due_at, recurring,
                          created_at, fingerprint, source_request_id
                """,
                (
                    reminder.reminder_id,
                    reminder.user_id,
                    reminder.content,
                    reminder.due_at,
                    reminder.recurring,
                    reminder.created_at,
                    reminder.fingerprint,
                    reminder.source_request_id,
                ),
            ).fetchone()
        return self._from_row(row), True

    def delete_for_user(self, user_id: str) -> int:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM ai_reminders WHERE user_id=%s",
                (user_id,),
            )
            return result.rowcount


def _clean_content(text: str, topic: str) -> str:
    if topic:
        return topic
    cleaned = _REMINDER_TRIGGERS.sub(" ", text)
    cleaned = _TIME_PATTERN.sub(" ", cleaned)
    cleaned = _MINUTES_FROM_NOW.sub(" ", cleaned)
    for word in ("请", "帮我", "记得", "我", "您", "每天", "今天", "明天", "明早", "后天"):
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", "", cleaned).strip("，。,、：:")
    return cleaned[:20] or "提醒事项"


def _hour_with_marker(hour: int, marker: str | None) -> int:
    if not marker:
        return hour
    if marker in {"下午", "傍晚", "晚上", "夜里"}:
        return (hour + 12) if hour < 12 else hour
    if marker == "中午":
        return 12 if hour < 12 else hour
    if marker in {"凌晨", "半夜"}:
        return 0 if hour == 12 else hour
    return hour


def parse_reminder_request(text: str, now: datetime | None = None) -> ReminderRequest | None:
    """Return a reminder request when the message contains a clear time."""
    if not _REMINDER_TRIGGERS.search(text or ""):
        return None
    now = (now or datetime.now(BEIJING_TZ)).replace(tzinfo=BEIJING_TZ)

    minutes_match = _MINUTES_FROM_NOW.search(text)
    if minutes_match:
        delta = timedelta(minutes=int(minutes_match.group(1)))
        content = _clean_content(text, "")
        return ReminderRequest(content=content, due_at=now + delta)

    time_match = _TIME_PATTERN.search(text)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or time_match.group(3) or 0)
    marker = re.search(r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)", text)
    hour = _hour_with_marker(hour, marker.group(1) if marker else None)
    if minute > 59 or hour > 23:
        return None

    day_offset = 0
    for word, offset in _DAY_MARKERS.items():
        if word in text:
            day_offset = offset
            break
    recurring = "daily" if "每天" in text else None

    due = (now + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if due <= now and recurring is None:
        due += timedelta(days=1)
    if recurring == "daily" and due <= now:
        due += timedelta(days=1)

    topic_match = _TOPICS.search(text)
    content = _clean_content(text, topic_match.group(1) if topic_match else "")
    return ReminderRequest(content=content, due_at=due, recurring=recurring)


class ReminderService:
    def __init__(self, repository: ReminderRepository | None = None) -> None:
        self.repository = repository or MemoryReminderRepository()

    def create(
        self,
        user_id: str,
        request: ReminderRequest,
        source_request_id: str = "",
        now: datetime | None = None,
    ) -> tuple[Reminder, bool]:
        """Create a reminder, idempotent for the same user+request window.

        ``now`` allows callers (and tests) to supply a fixed reference time;
        production callers leave it as ``None`` to use the current wall clock.
        """
        reference_now = (now or datetime.now(BEIJING_TZ)).replace(tzinfo=BEIJING_TZ)
        fingerprint = request.fingerprint()
        reminder = Reminder(
            reminder_id="rem-" + uuid.uuid4().hex[:16],
            user_id=user_id,
            content=request.content,
            due_at=request.due_at,
            recurring=request.recurring,
            created_at=reference_now,
            fingerprint=fingerprint,
            source_request_id=source_request_id,
        )
        return self.repository.create_if_absent(
            reminder,
            since=reference_now - timedelta(hours=24),
        )


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


"""Daily check-in reminder scheduling for an Enterprise WeChat employee group.

The reminder is sent through an internal group robot webhook.  There is no
customer-service 48-hour window for internal employee groups, so a configured
time slot can fire every day.  Sending is idempotent per (date, slot) and a
failed send is not marked as done, so the next scheduler tick may retry it.
"""


from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .wecom import WeComAppMessageSender, WeComWebhookSender


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CHECKIN_MESSAGE = (
    "小辽提醒：方便的时候花一分钟完成今天的情绪签到吧，"
    "记下此刻的心情就好，不用着急。"
)
_CHECKIN_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


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
        if not _CHECKIN_TIME_PATTERN.fullmatch(part):
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
