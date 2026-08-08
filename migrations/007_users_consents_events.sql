CREATE TABLE IF NOT EXISTS ai_users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL DEFAULT '',
    birth_year INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_consents (
    user_id TEXT PRIMARY KEY REFERENCES ai_users(user_id) ON DELETE CASCADE,
    personalization BOOLEAN NOT NULL DEFAULT FALSE,
    sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    version TEXT NOT NULL DEFAULT 'v1',
    granted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_conversation_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES ai_users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_ref TEXT NOT NULL,
    intent TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ai_conversation_events_user_idx
    ON ai_conversation_events (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_audit_logs (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ai_audit_logs_user_idx
    ON ai_audit_logs (user_id, created_at DESC);

REVOKE ALL ON ai_users, ai_consents, ai_conversation_events, ai_audit_logs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_users, ai_consents, ai_conversation_events, ai_audit_logs TO xiaoliao;
