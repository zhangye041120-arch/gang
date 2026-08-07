"""Local reminder parsing and storage for the companion agent.

The reminder tool only records what the user asked for; it never modifies the
system clock, schedules OS alarms, or pushes messages.  Persistence is
in-memory by default; production delivery still requires Java/WeCom push plus
an external durable store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
import threading
import uuid
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
_TIME_MARKERS = {
    "凌晨": 0,
    "半夜": 0,
    "早上": 7,
    "早晨": 7,
    "上午": 9,
    "中午": 12,
    "下午": 15,
    "傍晚": 18,
    "晚上": 20,
    "夜里": 22,
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


class MemoryReminderRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, list[Reminder]] = {}

    def list_for_user(self, user_id: str) -> list[Reminder]:
        with self._lock:
            return list(self._items.get(user_id, []))

    def add(self, reminder: Reminder) -> Reminder:
        with self._lock:
            self._items.setdefault(reminder.user_id, []).append(reminder)
        return reminder


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
    def __init__(self, repository: MemoryReminderRepository | None = None) -> None:
        self.repository = repository or MemoryReminderRepository()

    def create(
        self,
        user_id: str,
        request: ReminderRequest,
        source_request_id: str = "",
    ) -> tuple[Reminder, bool]:
        """Create a reminder, idempotent for the same user+request window."""
        fingerprint = request.fingerprint()
        existing = [
            item for item in self.repository.list_for_user(user_id)
            if item.fingerprint == fingerprint
            and item.due_at >= datetime.now(BEIJING_TZ) - timedelta(hours=24)
        ]
        if existing:
            return existing[0], False
        reminder = Reminder(
            reminder_id="rem-" + uuid.uuid4().hex[:16],
            user_id=user_id,
            content=request.content,
            due_at=request.due_at,
            recurring=request.recurring,
            created_at=datetime.now(BEIJING_TZ),
            fingerprint=fingerprint,
            source_request_id=source_request_id,
        )
        self.repository.add(reminder)
        return reminder, True
