CREATE TABLE IF NOT EXISTS ai_memory_consents (
    user_id TEXT PRIMARY KEY,
    personalization BOOLEAN NOT NULL DEFAULT FALSE,
    sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK (memory_type IN (
        'profile', 'family_relationship', 'interest_preference', 'key_event',
        'emotion_trend', 'action_summary', 'conceptualization_clue'
    )),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source_message_id TEXT NOT NULL,
    consent_scope TEXT NOT NULL CHECK (consent_scope = 'personalization'),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    embedding vector(1536)
);

CREATE UNIQUE INDEX IF NOT EXISTS ai_memories_active_source_idx
    ON ai_memories (user_id, memory_type, source_message_id, consent_scope)
    WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ai_memories_active_content_idx
    ON ai_memories (user_id, memory_type, content_hash)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ai_memories_user_updated_idx
    ON ai_memories (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS ai_memory_audit (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('correct', 'soft_delete', 'hard_delete')),
    created_at TIMESTAMPTZ NOT NULL
);

REVOKE ALL ON ai_memories, ai_memory_consents, ai_memory_audit FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_memories, ai_memory_consents, ai_memory_audit TO xiaoliao;
