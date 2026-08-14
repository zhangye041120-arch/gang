from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from xiaoliao_agent.reminders import (
    MemoryReminderRepository,
    PostgresReminderRepository,
    ReminderService,
    parse_reminder_request,
)


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 10, 0, tzinfo=TZ)


def test_daily_reminder_parses_topic_and_next_occurrence():
    request = parse_reminder_request("每天上午8点提醒我吃药", NOW)
    assert request is not None
    assert request.content == "吃药"
    assert request.recurring == "daily"
    assert request.due_at == datetime(2026, 8, 8, 8, 0, tzinfo=TZ)


def test_tomorrow_morning_reminder_parses():
    request = parse_reminder_request("明早9点提醒我去买菜", NOW)
    assert request is not None
    assert request.content == "买菜"
    assert request.recurring is None
    assert request.due_at == datetime(2026, 8, 8, 9, 0, tzinfo=TZ)


def test_passed_afternoon_time_rolls_to_tomorrow():
    late_now = datetime(2026, 8, 7, 16, 0, tzinfo=TZ)
    request = parse_reminder_request("下午3点提醒我复诊", late_now)
    assert request is not None
    assert request.content == "复诊"
    assert request.due_at == datetime(2026, 8, 8, 15, 0, tzinfo=TZ)


def test_minutes_from_now_reminder_parses():
    request = parse_reminder_request("5分钟后提醒我喝水", NOW)
    assert request is not None
    assert request.content == "喝水"
    assert request.due_at == NOW + timedelta(minutes=5)


def test_non_reminder_or_missing_time_returns_none():
    assert parse_reminder_request("今天天气怎么样", NOW) is None
    assert parse_reminder_request("提醒我吃药", NOW) is None


def test_service_is_idempotent_for_same_request_within_window():
    repository = MemoryReminderRepository()
    service = ReminderService(repository)
    request = parse_reminder_request("每天上午8点提醒我吃药", NOW)
    first, created_first = service.create("user-1", request, now=NOW)
    second, created_second = service.create("user-1", request, now=NOW)
    assert created_first is True
    assert created_second is False
    assert first.reminder_id == second.reminder_id
    assert len(repository.list_for_user("user-1")) == 1


def test_memory_repository_atomically_reports_duplicate():
    repository = MemoryReminderRepository()
    service = ReminderService(repository)
    request = parse_reminder_request("每天上午8点提醒我吃药", NOW)

    first, first_created = service.create("user-atomic", request, now=NOW)
    second, second_created = repository.create_if_absent(
        first,
        since=NOW - timedelta(hours=24),
    )

    assert first_created is True
    assert second_created is False
    assert second.reminder_id == first.reminder_id


def test_postgres_reminder_repository_requires_database_url():
    import pytest

    with pytest.raises(ValueError, match="database URL"):
        PostgresReminderRepository("")
