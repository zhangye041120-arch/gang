import uuid

import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.quality import InspectionLog, PostgresQualityRepository


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_quality_log_lesson_and_patch_round_trip():
    psycopg = pytest.importorskip("psycopg")
    repository = PostgresQualityRepository(DATABASE_URL)
    suffix = uuid.uuid4().hex
    request_id = "quality-" + suffix
    item = InspectionLog(
        request_id=request_id, message_id="synthetic-message", user_hash="synthetic-hash",
        candidate_reply_ref="sha256:synthetic", crisis_detected=False, safety_violation=False,
        intent_accurate=True, age_appropriate=True, cbt_appropriate=True, issues=[], latency_ms=1,
        main_model="test", inspector_model="test", prompt_version="test", input_tokens=None,
        output_tokens=None, total_tokens=None, cost=None, error_pattern="none", lesson_ref=None,
    )
    repository.write_log(item)
    repository.write_log(item)
    lesson = repository.add_lesson("synthetic lesson " + suffix, "none", "test")
    duplicate = repository.add_lesson("synthetic lesson " + suffix, "none", "test")
    assert duplicate.lesson_id == lesson.lesson_id
    repository.review_lesson(lesson.lesson_id, "approved", reviewer="test", reason="synthetic")
    patch = repository.create_patch("none", "synthetic patch " + suffix)
    reviewed = repository.review_patch(
        patch.patch_id, "approved", reviewer="test", reason="synthetic",
        test_report="synthetic-report", target_version="99.0.0",
    )
    assert reviewed.status == "approved"
    with psycopg.connect(DATABASE_URL) as connection:
        count = connection.execute("SELECT count(*) FROM ai_inspection_logs WHERE request_id=%s", (request_id,)).fetchone()[0]
        connection.execute("DELETE FROM ai_inspection_logs WHERE request_id=%s", (request_id,))
        connection.execute("DELETE FROM ai_lessons WHERE lesson_id=%s", (lesson.lesson_id,))
        connection.execute("DELETE FROM ai_prompt_patches WHERE patch_id=%s", (patch.patch_id,))
    assert count == 1
