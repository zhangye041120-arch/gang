import os

import pytest

from xiaoliao_agent.config import Settings

DATABASE_URL = Settings.from_env().knowledge_database_url or os.getenv("KNOWLEDGE_DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="KNOWLEDGE_DATABASE_URL 未配置")
def test_pgvector_database_is_ready():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DATABASE_URL) as connection:
        extension = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        table = connection.execute(
            "SELECT to_regclass('public.ai_knowledge_chunks')"
        ).fetchone()
        count = connection.execute(
            "SELECT count(*) FROM ai_knowledge_chunks"
        ).fetchone()
    assert extension and extension[0]
    assert table and table[0] == "ai_knowledge_chunks"
    assert count[0] >= 78
