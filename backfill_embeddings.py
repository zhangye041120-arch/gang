import argparse
import json

import psycopg

from xiaoliao_agent.config import Settings
from xiaoliao_agent.providers import EmbeddingError, OpenAICompatibleEmbeddingClient
from xiaoliao_agent.knowledge import KnowledgeBase


def main() -> int:
    parser = argparse.ArgumentParser(description="使用配置的 embedding 服务回填 CBT 知识向量")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    settings = Settings.from_env()
    if settings.embedding_provider == "none" or not settings.embedding_model:
        print("embedding 未配置：请设置 EMBEDDING_PROVIDER 和 EMBEDDING_MODEL")
        return 2
    if not settings.knowledge_database_url:
        print("KNOWLEDGE_DATABASE_URL 未配置")
        return 2
    if settings.embedding_provider.lower() != "qwen":
        print("当前只实现 qwen OpenAI 兼容 embedding 提供商")
        return 2

    kb = KnowledgeBase.from_files(
        settings.knowledge_path,
        settings.lessons_path,
        version=settings.knowledge_version,
    )
    chunks_by_id = {chunk.chunk_id: chunk for chunk in kb.chunks}
    with psycopg.connect(settings.knowledge_database_url) as connection:
        rows = connection.execute(
            "SELECT id FROM ai_knowledge_chunks WHERE embedding IS NULL AND version=%s",
            (settings.knowledge_version,),
        ).fetchall()
        missing_ids = [str(row[0]) for row in rows]
    print(f"知识块总数={len(kb.chunks)}，缺向量={len(missing_ids)}")
    if not missing_ids:
        print("所有知识块均已 embedding，无需回填。")
        return 0

    client = OpenAICompatibleEmbeddingClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.timeout_seconds,
    )
    missing_chunks = [chunks_by_id[chunk_id] for chunk_id in missing_ids if chunk_id in chunks_by_id]
    if len(missing_chunks) != len(missing_ids):
        print(f"警告：{len(missing_ids) - len(missing_chunks)} 个缺向量 id 不在当前知识文件里")
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(missing_chunks), args.batch_size):
            batch = missing_chunks[start : start + args.batch_size]
            vectors.extend(client.embed([chunk.heading + "\n" + chunk.content for chunk in batch]))
            print(f"embedded={len(vectors)}/{len(missing_chunks)}")
    except EmbeddingError as exc:
        print(f"embedding 失败：{exc}")
        return 1

    with psycopg.connect(settings.knowledge_database_url) as connection:
        for chunk, vector in zip(missing_chunks, vectors):
            connection.execute(
                "UPDATE ai_knowledge_chunks SET embedding=%s::vector WHERE id=%s AND version=%s",
                (json.dumps(vector, separators=(",", ":")), chunk.chunk_id, chunk.version),
            )
    print(f"backfilled={len(vectors)} dimension={settings.embedding_dimension}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
