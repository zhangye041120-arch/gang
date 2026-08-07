from datetime import datetime, timedelta, timezone
import uuid

import pytest

from xiaoliao_agent.actions import ActionEvent, ActionService, PostgresActionRepository
from xiaoliao_agent.config import Settings


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_action_event_round_trip_and_idempotency():
    psycopg = pytest.importorskip("psycopg")
    user_id = f"action-integration-{uuid.uuid4().hex}"
    repository = PostgresActionRepository(DATABASE_URL)
    service = ActionService(repository)
    recommendation = service.recommend(user_id, "session", {
        "type": "miniprogram",
        "module": "M1",
        "page": "/pages/checkin/index",
        "params": {},
        "reason": "integration_test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }, source_message_id="synthetic-message")
    service.record_event(ActionEvent("accept-" + uuid.uuid4().hex, recommendation.recommendation_id, user_id, "M1", "accepted", datetime.now(timezone.utc), {}))
    event_id = "complete-" + uuid.uuid4().hex
    completed = service.record_event(ActionEvent(event_id, recommendation.recommendation_id, user_id, "M1", "completed", datetime.now(timezone.utc), {"activity": "合成签到", "effort": "完成一次合成记录"}))
    duplicate = service.record_event(ActionEvent(event_id, recommendation.recommendation_id, user_id, "M1", "completed", datetime.now(timezone.utc), {"activity": "不应覆盖"}))
    assert completed.status == "completed"
    assert duplicate.status == "completed"
    assert "合成签到" in service.feedback_for(recommendation.recommendation_id)
    with psycopg.connect(DATABASE_URL) as connection:
        count = connection.execute("SELECT count(*) FROM ai_action_events WHERE recommendation_id=%s", (recommendation.recommendation_id,)).fetchone()[0]
        connection.execute("DELETE FROM ai_action_events WHERE recommendation_id=%s", (recommendation.recommendation_id,))
        connection.execute("DELETE FROM ai_action_recommendations WHERE recommendation_id=%s", (recommendation.recommendation_id,))
    assert count == 2
