from datetime import datetime
import uuid

import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.reminders import (
    BEIJING_TZ,
    PostgresReminderRepository,
    ReminderService,
    parse_reminder_request,
)


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_reminder_survives_repository_recreation():
    user_id = "reminder-db-" + uuid.uuid4().hex
    now = datetime(2026, 8, 14, 9, 0, tzinfo=BEIJING_TZ)
    request = parse_reminder_request("每天上午8点提醒我吃药", now)
    first_service = ReminderService(PostgresReminderRepository(DATABASE_URL))

    reminder, created = first_service.create(user_id, request, source_request_id="req-1", now=now)
    second_service = ReminderService(PostgresReminderRepository(DATABASE_URL))
    duplicate, duplicate_created = second_service.create(
        user_id,
        request,
        source_request_id="req-1",
        now=now,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate.reminder_id == reminder.reminder_id
    assert second_service.repository.list_for_user(user_id)[0].content == "吃药"
    second_service.repository.delete_for_user(user_id)
