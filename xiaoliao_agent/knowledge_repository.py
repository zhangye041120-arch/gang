from dataclasses import dataclass, field
from typing import Protocol

from .knowledge import Chunk


class KnowledgeRepository(Protocol):
    def upsert(self, chunk: Chunk) -> str:
        """Return inserted, updated, or skipped."""


@dataclass
class MemoryKnowledgeRepository:
    """Offline repository used for unit tests and import dry runs."""

    rows: dict[str, Chunk] = field(default_factory=dict)

    def upsert(self, chunk: Chunk) -> str:
        current = self.rows.get(chunk.chunk_id)
        if current is not None and current.content_hash == chunk.content_hash:
            return "skipped"
        self.rows[chunk.chunk_id] = chunk
        return "updated" if current is not None else "inserted"


class PostgresKnowledgeRepository:
    """Small production adapter; connection setup remains outside the Agent.

    The migration owns the vector dimension. The importer can therefore be run
    without this adapter when PostgreSQL or pgvector is unavailable.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("knowledge database URL is required")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional integration
            raise RuntimeError("psycopg is required for PostgreSQL import") from exc
        self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)

    def upsert(self, chunk: Chunk) -> str:  # pragma: no cover - requires PostgreSQL
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO ai_knowledge_chunks
                    (id, source, version, heading_path, content, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, version, content_hash) DO NOTHING
                RETURNING id
                """,
                (
                    chunk.chunk_id,
                    chunk.source,
                    chunk.version,
                    list(chunk.heading_path),
                    chunk.content,
                    chunk.content_hash,
                ),
            ).fetchone()
            return "inserted" if row else "skipped"

    def vector_search(self, vector: list[float], top_k: int, *, version: str = "v1") -> list[tuple[str, float]]:
        import json

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, 1 - (embedding <=> %s::vector) AS score
                FROM ai_knowledge_chunks
                WHERE version = %s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (json.dumps(vector, separators=(",", ":")), version,
                 json.dumps(vector, separators=(",", ":")), top_k),
            ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]
