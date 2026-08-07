-- 006: 记忆向量索引（embedding 列已在 003 中定义）
-- pgvector 的 IVFFlat 索引在数据量达几百条后效率显著提升

CREATE INDEX IF NOT EXISTS ai_memories_embedding_idx
    ON ai_memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10)
    WHERE embedding IS NOT NULL AND deleted_at IS NULL;

REVOKE ALL ON ai_memories FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_memories TO xiaoliao;
