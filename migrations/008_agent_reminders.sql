CREATE TABLE IF NOT EXISTS ai_reminders (
    reminder_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES ai_users(user_id) ON DELETE CASCADE,
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 100),
    due_at TIMESTAMPTZ NOT NULL,
    recurring TEXT CHECK (recurring IS NULL OR recurring = 'daily'),
    created_at TIMESTAMPTZ NOT NULL,
    fingerprint TEXT NOT NULL,
    source_request_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'cancelled')),
    UNIQUE (user_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS ai_reminders_due_idx
    ON ai_reminders (due_at)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ai_reminders_user_idx
    ON ai_reminders (user_id, created_at DESC);

REVOKE ALL ON ai_reminders FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_reminders TO xiaoliao;
