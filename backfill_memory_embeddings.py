"""Backfill embedding vectors for user memories that lack one.

Usage:
    python backfill_memory_embeddings.py              # backfill all missing
    python backfill_memory_embeddings.py --limit 50   # only 50 rows

Requires:
    - KNOWLEDGE_DATABASE_URL pointing to the pgvector database
    - EMBEDDING_PROVIDER=qwen and EMBEDDING_MODEL set in .env
"""

import argparse
import json
import sys

import psycopg

from xiaoliao_agent.config import Settings
from xiaoliao_agent.embeddings import EmbeddingError, OpenAICompatibleEmbeddingClient
from xiaoliao_agent.memory_repository import PostgresMemoryRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="回填用户记忆向量")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="上限（0=全部缺失行）")
    args = parser.parse_args()

    settings = Settings.from_env()
    if settings.embedding_provider == "none" or not settings.embedding_model:
        print("embedding 未配置：请设置 EMBEDDING_PROVIDER 和 EMBEDDING_MODEL")
        return 2
    if not settings.knowledge_database_url:
        print("KNOWLEDGE_DATABASE_URL 未配置")
        return 2

    repo = PostgresMemoryRepository(settings.knowledge_database_url)
    with_emb, without_emb = repo.count_embeddings()
    print(f"当前状态: with_embedding={with_emb}, without_embedding={without_emb}")

    client = OpenAICompatibleEmbeddingClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.timeout_seconds,
    )

    missing_ids = repo.find_embeddings_missing(limit=args.limit or 10_000)
    if not missing_ids:
        print("所有活跃记忆均已embedding，无需回填。")
        return 0

    print(f"需要回填 {len(missing_ids)} 条记忆")

    # Fetch content for each missing memory_id
    mem_contents: dict[str, str] = {}
    with psycopg.connect(settings.knowledge_database_url, connect_timeout=5) as connection:
        for start in range(0, len(missing_ids), 500):
            batch = missing_ids[start : start + 500]
            rows = connection.execute(
                "SELECT memory_id, content FROM ai_memories WHERE memory_id = ANY(%s) AND deleted_at IS NULL",
                (batch,),
            ).fetchall()
            for row in rows:
                mem_contents[row[0]] = row[1]

    print(f"获取到 {len(mem_contents)} 条内容")

    count = 0
    ids_list = list(mem_contents.keys())
    try:
        for start in range(0, len(ids_list), args.batch_size):
            batch_ids = ids_list[start : start + args.batch_size]
            texts = [mem_contents[mid] for mid in batch_ids]
            vectors = client.embed(texts)
            for mid, vector in zip(batch_ids, vectors):
                repo.update_embedding(mid, vector)
            count += len(vectors)
            print(f"backfilled={count}/{len(ids_list)}")
    except EmbeddingError as exc:
        print(f"embedding 失败：{exc}")
        return 1

    print(f"完成: backfilled={count} dimension={settings.embedding_dimension}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
