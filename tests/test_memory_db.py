import uuid

import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.memory import MemoryCandidate, MemoryService
from xiaoliao_agent.memory_repository import PostgresMemoryRepository


DATABASE_URL = Settings.from_env().knowledge_database_url


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_memory_consent_write_correct_and_delete_round_trip():
    psycopg = pytest.importorskip("psycopg")
    user_id = f"memory-integration-{uuid.uuid4().hex}"
    repository = PostgresMemoryRepository(DATABASE_URL)
    service = MemoryService(repository)
    service.set_consent(user_id, personalization=True)
    record = service.save_candidate(user_id, MemoryCandidate(
        memory_type="interest_preference",
        content="合成测试偏好",
        confidence=0.99,
        source_message_id="synthetic-message",
        explicitly_stated=True,
    ))
    duplicate = service.save_candidate(user_id, MemoryCandidate(
        memory_type="interest_preference",
        content="合成测试偏好",
        confidence=0.99,
        source_message_id="another-message",
        explicitly_stated=True,
    ))
    assert duplicate.memory_id == record.memory_id
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE ai_memories SET embedding=array_fill(0.1::real, ARRAY[1536])::vector WHERE memory_id=%s",
            (record.memory_id,),
        )
    service.correct(user_id, record.memory_id, "合成测试纠正")
    with psycopg.connect(DATABASE_URL) as connection:
        embedding_is_null = connection.execute(
            "SELECT embedding IS NULL FROM ai_memories WHERE memory_id=%s",
            (record.memory_id,),
        ).fetchone()[0]
    assert embedding_is_null
    service.soft_delete(user_id, record.memory_id)
    tombstone = repository.get(user_id, record.memory_id, include_deleted=True)
    assert tombstone.content == ""
    service.hard_delete(user_id, record.memory_id)
    service.hard_delete(user_id, record.memory_id)
    assert repository.list_for_user(user_id, include_deleted=True) == []
    with psycopg.connect(DATABASE_URL) as connection:
        audit_columns = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='ai_memory_audit'"
        ).fetchall()
        connection.execute("DELETE FROM ai_memory_audit WHERE user_id=%s", (user_id,))
        connection.execute("DELETE FROM ai_memory_consents WHERE user_id=%s", (user_id,))
    assert "content" not in {row[0] for row in audit_columns}


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_memory_vector_index_is_applied():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='ai_memories' AND indexname='ai_memories_embedding_idx'"
        ).fetchone()
    assert row and row[0] == "ai_memories_embedding_idx"


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_postgres_memory_embedding_round_trip_and_vector_search():
    psycopg = pytest.importorskip("psycopg")
    user_id = f"memory-vector-{uuid.uuid4().hex}"
    repository = PostgresMemoryRepository(DATABASE_URL)
    service = MemoryService(repository)
    service.set_consent(user_id, personalization=True)
    record = service.save_candidate(user_id, MemoryCandidate(
        memory_type="interest_preference",
        content="合成向量偏好",
        confidence=0.99,
        source_message_id="vector-message",
        explicitly_stated=True,
    ))
    vector = [0.01] * 1536
    repository.update_embedding(record.memory_id, vector)
    with_emb, without_emb = repository.count_embeddings()
    assert with_emb >= 1
    assert record.memory_id not in repository.find_embeddings_missing(limit=100)
    hits = repository.vector_search(user_id, vector, top_k=3)
    assert hits and hits[0][0] == record.memory_id
    service.hard_delete(user_id, record.memory_id)
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute("DELETE FROM ai_memory_audit WHERE user_id=%s", (user_id,))
        connection.execute("DELETE FROM ai_memory_consents WHERE user_id=%s", (user_id,))
