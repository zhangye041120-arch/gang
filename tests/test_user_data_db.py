import uuid

import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.user_data import PostgresUserRepository, UserDataService


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_user_consent_events_and_delete_round_trip():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DATABASE_URL) as connection:
        has_table = connection.execute(
            "SELECT to_regclass('ai_users') IS NOT NULL"
        ).fetchone()[0]
    if not has_table:
        pytest.skip("007_users_consents_events.sql 尚未执行")
    user_id = f"user-data-{uuid.uuid4().hex}"
    service = UserDataService(PostgresUserRepository(DATABASE_URL))
    service.get_or_create_user(user_id, nickname="synthetic")
    service.set_consent(user_id, personalization=True)
    service.log_conversation_pair(
        user_id,
        user_text="合成消息",
        reply="合成回复",
        intent="chat",
        request_id=f"req-{uuid.uuid4().hex}",
    )
    summary = service.summary(user_id)
    assert summary["personalization"] is True
    assert len(summary["recent_events"]) == 2
    service.delete_user(user_id)
    assert service.repository.list_conversation_events(user_id) == []
    assert service.consent_for(user_id) == (False, False)
