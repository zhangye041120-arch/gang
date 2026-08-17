from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
import uuid

import pytest

from xiaoliao_agent.actions import ActionEvent, ActionService, PostgresActionRepository
from xiaoliao_agent.config import Settings


DATABASE_URL = Settings.from_env().knowledge_database_url


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class RecordingActionConnection:
    def __init__(self, recommendation):
        self.recommendation = recommendation
        self.statements = []

    def execute(self, statement, parameters=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, parameters))
        if "FROM ai_action_events" in normalized:
            return Result(None)
        if "FROM ai_action_recommendations" in normalized:
            return Result(tuple(self.recommendation.__dict__.values()))
        if normalized.startswith("UPDATE ai_action_recommendations"):
            updated = tuple({**self.recommendation.__dict__, "status": "completed"}.values())
            return Result(updated)
        return Result()


def test_postgres_action_event_locks_event_id_before_duplicate_check():
    from contextlib import contextmanager
    from xiaoliao_agent.actions import ActionRecommendation

    now = datetime.now(timezone.utc)
    recommendation = ActionRecommendation(
        "recommendation-1", "user-1", "session-1", "M1", {}, "reason",
        "accepted", "source-1", now + timedelta(minutes=5), now,
    )
    connection = RecordingActionConnection(recommendation)
    repository = object.__new__(PostgresActionRepository)

    @contextmanager
    def connect():
        yield connection

    repository._connect = connect
    repository.apply_event(ActionEvent(
        "event-1", recommendation.recommendation_id, "user-1", "M1",
        "completed", now, {}, "完成签到", "fingerprint-1",
    ))

    assert "pg_advisory_xact_lock" in connection.statements[0][0]


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
    event = ActionEvent("complete-" + uuid.uuid4().hex, recommendation.recommendation_id, user_id, "M1", "completed", datetime.now(timezone.utc), {"activity": "合成签到", "effort": "完成一次合成记录"})
    completed = service.record_event(event)
    duplicate = service.record_event(event)
    assert completed.status == "completed"
    assert duplicate.status == "completed"
    assert "情绪签到" in service.feedback_for(recommendation.recommendation_id)
    with psycopg.connect(DATABASE_URL) as connection:
        count = connection.execute("SELECT count(*) FROM ai_action_events WHERE recommendation_id=%s", (recommendation.recommendation_id,)).fetchone()[0]
        connection.execute("DELETE FROM ai_action_events WHERE recommendation_id=%s", (recommendation.recommendation_id,))
        connection.execute("DELETE FROM ai_action_recommendations WHERE recommendation_id=%s", (recommendation.recommendation_id,))
        connection.execute("DELETE FROM ai_users WHERE user_id=%s", (user_id,))
    assert count == 2


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_action_event_is_atomic_across_concurrent_workers():
    psycopg = pytest.importorskip("psycopg")
    user_id = f"action-concurrent-{uuid.uuid4().hex}"
    repository = PostgresActionRepository(DATABASE_URL)
    service = ActionService(repository)
    recommendation = service.recommend(user_id, "session", {
        "type": "miniprogram",
        "module": "M1",
        "page": "/pages/checkin/index",
        "params": {},
        "reason": "concurrency_test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }, source_message_id="concurrent-source")
    service.record_event(ActionEvent(
        "accept-" + uuid.uuid4().hex,
        recommendation.recommendation_id,
        user_id,
        "M1",
        "accepted",
        datetime.now(timezone.utc),
        {},
    ))
    event = ActionEvent(
        "complete-" + uuid.uuid4().hex,
        recommendation.recommendation_id,
        user_id,
        "M1",
        "completed",
        datetime.now(timezone.utc),
        {"activity": "并发签到", "effort": "只记录一次"},
    )
    barrier = Barrier(4)

    def apply(_index):
        barrier.wait()
        return service.record_event_with_status(event)[1]

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            created = list(executor.map(apply, range(4)))
        assert created.count(True) == 1
        assert created.count(False) == 3
        with psycopg.connect(DATABASE_URL) as connection:
            count = connection.execute(
                "SELECT count(*) FROM ai_action_events WHERE recommendation_id=%s",
                (recommendation.recommendation_id,),
            ).fetchone()[0]
        assert count == 2
    finally:
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM ai_action_events WHERE recommendation_id=%s",
                (recommendation.recommendation_id,),
            )
            connection.execute(
                "DELETE FROM ai_action_recommendations WHERE recommendation_id=%s",
                (recommendation.recommendation_id,),
            )
            connection.execute("DELETE FROM ai_users WHERE user_id=%s", (user_id,))
