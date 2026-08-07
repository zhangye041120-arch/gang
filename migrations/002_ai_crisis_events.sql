CREATE TABLE IF NOT EXISTS ai_crisis_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level = 'crisis'),
    evidence_code TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'notified', 'reviewed', 'failed')),
    prompt_version TEXT NOT NULL,
    response_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ai_crisis_events_created_at_idx
    ON ai_crisis_events (created_at);

REVOKE ALL ON ai_crisis_events FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON ai_crisis_events TO xiaoliao;

COMMENT ON TABLE ai_crisis_events IS
    'High-privilege crisis audit records. Default retention is 365 days; purge requires an approved scheduled job.';
