-- Embedding dimension must match EMBEDDING_DIMENSION in .env (default: 1536).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS ai_knowledge_chunks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    version TEXT NOT NULL,
    heading_path TEXT[] NOT NULL DEFAULT '{}',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, version, content_hash)
);

CREATE INDEX IF NOT EXISTS ai_knowledge_chunks_source_version_idx
    ON ai_knowledge_chunks (source, version);

CREATE INDEX IF NOT EXISTS ai_knowledge_chunks_embedding_idx
    ON ai_knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
