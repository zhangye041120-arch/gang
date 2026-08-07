CREATE TABLE IF NOT EXISTS ai_inspection_logs (
    request_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    user_hash TEXT NOT NULL,
    candidate_reply_ref TEXT NOT NULL,
    crisis_detected BOOLEAN NOT NULL,
    safety_violation BOOLEAN NOT NULL,
    intent_accurate BOOLEAN NOT NULL,
    age_appropriate BOOLEAN NOT NULL,
    cbt_appropriate BOOLEAN NOT NULL,
    issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms INTEGER NOT NULL,
    main_model TEXT NOT NULL,
    inspector_model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost NUMERIC,
    error_pattern TEXT NOT NULL,
    lesson_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ai_inspection_logs_pattern_idx
    ON ai_inspection_logs (error_pattern, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_lessons (
    lesson_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    error_pattern TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewer TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ai_prompt_patches (
    patch_id TEXT PRIMARY KEY,
    error_pattern TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewer TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    test_report TEXT NOT NULL DEFAULT '',
    target_version TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ai_prompt_versions (
    prompt_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    owner TEXT NOT NULL,
    changelog TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prompt_id, semantic_version)
);

REVOKE ALL ON ai_inspection_logs, ai_lessons, ai_prompt_patches, ai_prompt_versions FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON ai_inspection_logs, ai_lessons, ai_prompt_patches, ai_prompt_versions TO xiaoliao;
