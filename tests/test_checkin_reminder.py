from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from xiaoliao_agent.reminders import (
    CheckinReminderPolicy,
    CheckinUserSchedule,
    DailyCheckinService,
    checkin_policy_from_settings,
    load_checkin_user_schedules,
    parse_schedule_times,
)
from xiaoliao_agent.config import Settings


TZ = ZoneInfo("Asia/Shanghai")


class FakeSender:
    def __init__(self):
        self.calls = []
        self.fail = False

    def send_text(
        self,
        content,
        *,
        mentioned_user_ids=(),
        mentioned_mobiles=(),
        mention_all=False,
    ):
        if self.fail:
            raise RuntimeError("webhook down")
        self.calls.append({
            "content": content,
            "mentioned_user_ids": tuple(mentioned_user_ids),
            "mentioned_mobiles": tuple(mentioned_mobiles),
            "mention_all": mention_all,
        })


class FakeAppSender:
    def __init__(self):
        self.calls = []
        self.fail = False

    def send_text(self, user_id, content):
        if self.fail:
            raise RuntimeError("app message down")
        self.calls.append({"user_id": user_id, "content": content})


def test_parse_schedule_times_dedup_and_sort():
    assert parse_schedule_times("15:00,09:00,09:00") == ("09:00", "15:00")
    with pytest.raises(ValueError):
        parse_schedule_times("9点")


def test_policy_from_settings_disabled_by_default():
    settings = Settings()
    policy = checkin_policy_from_settings(settings)
    assert policy.enabled is False
    assert policy.times == ("09:00",)
    assert policy.webhook_url == ""


def test_service_sends_once_and_deduplicates_same_slot():
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        message="该签到啦",
        catch_up_minutes=15,
    )
    sender = FakeSender()
    service = DailyCheckinService(policy, sender=sender)
    now = datetime(2026, 8, 8, 9, 5, tzinfo=TZ)
    first = service.run_due(now)
    second = service.run_due(now)
    assert first == [{
        "status": "sent",
        "target": "group",
        "day": "2026-08-08",
        "slot": "09:00",
    }]
    assert second[0]["status"] == "duplicate"
    assert len(sender.calls) == 1
    assert sender.calls[0]["content"] == "该签到啦"


def test_service_skips_slots_outside_catch_up_window():
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        catch_up_minutes=15,
    )
    service = DailyCheckinService(policy, sender=FakeSender())
    assert service.run_due(datetime(2026, 8, 8, 9, 16, tzinfo=TZ)) == []


def test_failed_send_is_not_marked_sent_and_can_retry():
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        catch_up_minutes=15,
    )
    sender = FakeSender()
    service = DailyCheckinService(policy, sender=sender)
    now = datetime(2026, 8, 8, 9, 5, tzinfo=TZ)
    sender.fail = True
    failed = service.run_due(now)
    assert failed[0]["status"] == "failed"
    sender.fail = False
    retried = service.run_due(now)
    assert retried[0]["status"] == "sent"
    assert len(sender.calls) == 1


def test_mentions_are_passed_to_sender():
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        mention_all=True,
        mentioned_user_ids=("u1",),
        mentioned_mobiles=("13800000000",),
        catch_up_minutes=15,
    )
    sender = FakeSender()
    service = DailyCheckinService(policy, sender=sender)
    service.run_due(datetime(2026, 8, 8, 9, 5, tzinfo=TZ))
    call = sender.calls[0]
    assert call["mention_all"] is True
    assert call["mentioned_user_ids"] == ("u1",)
    assert call["mentioned_mobiles"] == ("13800000000",)


def test_disabled_and_missing_webhook_are_blocked():
    disabled = DailyCheckinService(CheckinReminderPolicy(enabled=False))
    result = disabled.run_due(datetime(2026, 8, 8, 9, 5, tzinfo=TZ))
    assert result[0]["reason"] == "global_disabled"

    no_webhook = DailyCheckinService(
        CheckinReminderPolicy(enabled=True, times=("09:00",))
    )
    result = no_webhook.run_due(datetime(2026, 8, 8, 9, 5, tzinfo=TZ))
    assert result[0]["reason"] == "webhook_not_configured"


def test_sqlite_repository_survives_service_restart(tmp_path):
    state_path = tmp_path / "checkin.sqlite3"
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        catch_up_minutes=15,
        state_path=str(state_path),
    )
    first_service = DailyCheckinService(policy, sender=FakeSender())
    now = datetime(2026, 8, 8, 9, 5, tzinfo=TZ)
    assert first_service.run_due(now)[0]["status"] == "sent"
    second_service = DailyCheckinService(policy, sender=FakeSender())
    assert second_service.run_due(now)[0]["status"] == "duplicate"
    assert state_path.exists()


def test_load_user_schedules_from_json(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text(
        """
        {
          "users": [
            {"user_id": "zhangsan", "times": ["08:00"]},
            {"user_id": "lisi", "times": ["09:30", "15:00"]}
          ]
        }
        """,
        encoding="utf-8",
    )
    schedules = load_checkin_user_schedules(path)
    assert schedules == (
        CheckinUserSchedule(user_id="zhangsan", times=("08:00",)),
        CheckinUserSchedule(
            user_id="lisi",
            times=("09:30", "15:00"),
        ),
    )
    assert schedules[1].target_key == "user:lisi"


def test_policy_from_settings_loads_user_schedules(tmp_path):
    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        '{"users": [{"user_id": "zhangsan", "times": ["08:00"]}]}',
        encoding="utf-8",
    )
    settings = Settings(wecom_checkin_group_user_schedule_path=str(schedule_path))
    policy = checkin_policy_from_settings(settings)
    assert len(policy.user_schedules) == 1
    assert policy.user_schedules[0].times == ("08:00",)


def test_service_sends_per_user_at_personalized_time():
    policy = CheckinReminderPolicy(
        enabled=True,
        times=("09:00",),
        user_schedules=(
            CheckinUserSchedule(user_id="zhangsan", times=("08:00",)),
            CheckinUserSchedule(user_id="lisi", times=("09:30",)),
        ),
        catch_up_minutes=15,
    )
    sender = FakeAppSender()
    service = DailyCheckinService(policy, sender=sender)
    events = service.run_due(datetime(2026, 8, 8, 8, 5, tzinfo=TZ))
    assert events == [{
        "status": "sent",
        "target": "user:zhangsan",
        "user_id": "zhangsan",
        "day": "2026-08-08",
        "slot": "08:00",
    }]
    assert sender.calls[0]["user_id"] == "zhangsan"
    assert sender.calls[0]["content"] == policy.message

    events = service.run_due(datetime(2026, 8, 8, 9, 35, tzinfo=TZ))
    assert events[0]["target"] == "user:lisi"
    assert sender.calls[1]["user_id"] == "lisi"


def test_user_schedule_is_idempotent_per_user_and_slot():
    policy = CheckinReminderPolicy(
        enabled=True,
        user_schedules=(
            CheckinUserSchedule(user_id="zhangsan", times=("08:00", "15:00")),
        ),
        catch_up_minutes=15,
    )
    sender = FakeAppSender()
    service = DailyCheckinService(policy, sender=sender)
    morning = datetime(2026, 8, 8, 8, 5, tzinfo=TZ)
    assert service.run_due(morning)[0]["status"] == "sent"
    assert service.run_due(morning)[0]["status"] == "duplicate"
    afternoon = datetime(2026, 8, 8, 15, 5, tzinfo=TZ)
    assert service.run_due(afternoon)[0]["status"] == "sent"
    assert len(sender.calls) == 2


def test_user_schedule_validation_rejects_missing_target(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"users": [{"times": ["08:00"]}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="user_id"):
        load_checkin_user_schedules(path)


def test_user_schedule_requires_app_message_credentials():
    policy = CheckinReminderPolicy(
        enabled=True,
        user_schedules=(CheckinUserSchedule(user_id="zhangsan", times=("08:00",)),),
        catch_up_minutes=15,
    )
    service = DailyCheckinService(policy)
    result = service.run_due(datetime(2026, 8, 8, 8, 5, tzinfo=TZ))
    assert result[0]["reason"] == "app_message_not_configured"


def test_sqlite_user_schedule_survives_service_restart(tmp_path):
    state_path = tmp_path / "checkin.sqlite3"
    policy = CheckinReminderPolicy(
        enabled=True,
        user_schedules=(
            CheckinUserSchedule(user_id="zhangsan", times=("08:00",)),
        ),
        catch_up_minutes=15,
        state_path=str(state_path),
    )
    now = datetime(2026, 8, 8, 8, 5, tzinfo=TZ)
    first_service = DailyCheckinService(policy, sender=FakeAppSender())
    assert first_service.run_due(now)[0]["status"] == "sent"
    second_service = DailyCheckinService(policy, sender=FakeAppSender())
    assert second_service.run_due(now)[0]["status"] == "duplicate"


def test_env_example_and_doc_cover_checkin_reminder():
    project_root = Path(__file__).resolve().parents[1]
    env = (project_root / ".env.example").read_text(encoding="utf-8")
    doc = (project_root / "docs" / "签到提醒策略.md").read_text(encoding="utf-8")
    example = project_root / "checkin_user_schedule.example.json"
    assert "WECOM_CHECKIN_REMINDER_ENABLED" in env
    assert "WECOM_CHECKIN_GROUP_WEBHOOK" in doc
    assert "WECOM_CHECKIN_GROUP_USER_SCHEDULE_PATH" in env
    assert example.exists()
