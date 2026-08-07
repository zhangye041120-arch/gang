import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.crisis_repository import PostgresCrisisEventRepository, new_crisis_event


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_crisis_event_repository_round_trip_without_sensitive_text():
    psycopg = pytest.importorskip("psycopg")
    repository = PostgresCrisisEventRepository(DATABASE_URL)
    event = new_crisis_event(
        user_id="integration-user",
        session_id="integration-session",
        evidence_code="C_TEST",
        route="unconfigured",
        prompt_version="test",
        response_version="test",
    )
    repository.add(event)
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT evidence_code, route FROM ai_crisis_events WHERE event_id = %s",
            (event.event_id,),
        ).fetchone()
        connection.execute("DELETE FROM ai_crisis_events WHERE event_id = %s", (event.event_id,))
    assert row == ("C_TEST", "unconfigured")
