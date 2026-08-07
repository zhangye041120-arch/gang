CREATE TABLE IF NOT EXISTS ai_action_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    module TEXT NOT NULL CHECK (module IN ('M1', 'M2', 'M3', 'M5')),
    action_json JSONB NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('recommended', 'accepted', 'completed', 'declined', 'expired')),
    source_message_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    declined_reason TEXT NOT NULL DEFAULT '',
    feedback TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ai_action_recommendations_user_idx
    ON ai_action_recommendations (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_action_events (
    event_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES ai_action_recommendations(recommendation_id),
    user_id TEXT NOT NULL,
    module TEXT NOT NULL CHECK (module IN ('M1', 'M2', 'M3', 'M5')),
    event_type TEXT NOT NULL CHECK (event_type IN ('accepted', 'completed', 'declined', 'expired')),
    occurred_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ai_action_events_recommendation_idx
    ON ai_action_events (recommendation_id, occurred_at);

REVOKE ALL ON ai_action_recommendations, ai_action_events FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_action_recommendations, ai_action_events TO xiaoliao;
