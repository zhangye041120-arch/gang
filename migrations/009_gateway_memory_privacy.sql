ALTER TABLE ai_memories
    ADD COLUMN IF NOT EXISTS memory_key TEXT,
    ADD COLUMN IF NOT EXISTS supersedes_memory_id TEXT,
    ADD COLUMN IF NOT EXISTS source_type TEXT,
    ADD COLUMN IF NOT EXISTS sensitive BOOLEAN;

ALTER TABLE ai_action_events
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS request_fingerprint TEXT;

ALTER TABLE ai_inspection_logs
    ADD COLUMN IF NOT EXISTS subject_hmac TEXT;

CREATE TABLE IF NOT EXISTS ai_privacy_deletion_audits (
    request_id TEXT PRIMARY KEY,
    subject_hmac TEXT NOT NULL,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    database_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('completed', 'redis_pending', 'failed')),
    requested_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ai_privacy_tombstones (
    subject_hmac TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('deleting', 'deleted')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_lesson_subjects (
    lesson_id TEXT NOT NULL REFERENCES ai_lessons(lesson_id) ON DELETE CASCADE,
    subject_hmac TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lesson_id, subject_hmac)
);

CREATE INDEX IF NOT EXISTS ai_lesson_subjects_subject_idx
    ON ai_lesson_subjects (subject_hmac);

INSERT INTO ai_users (user_id, nickname, birth_year, status, created_at, updated_at)
SELECT DISTINCT subject.user_id, '', NULL::INTEGER, 'active', now(), now()
FROM (
    SELECT user_id FROM ai_memory_consents
    UNION ALL SELECT user_id FROM ai_memories
    UNION ALL SELECT user_id FROM ai_action_recommendations
    UNION ALL SELECT user_id FROM ai_action_events
    UNION ALL SELECT user_id FROM ai_crisis_events
    UNION ALL SELECT user_id FROM ai_reminders
) AS subject
WHERE subject.user_id IS NOT NULL AND subject.user_id <> ''
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO ai_consents
    (user_id, personalization, sensitive, version, granted_at, revoked_at, updated_at)
SELECT user_id,
       personalization,
       CASE WHEN personalization THEN sensitive ELSE FALSE END,
       'legacy-memory-v1',
       CASE WHEN personalization THEN updated_at ELSE NULL END,
       CASE WHEN personalization THEN NULL ELSE updated_at END,
       updated_at
FROM ai_memory_consents
ON CONFLICT (user_id) DO UPDATE SET
    personalization = EXCLUDED.personalization,
    sensitive = EXCLUDED.sensitive,
    version = EXCLUDED.version,
    granted_at = EXCLUDED.granted_at,
    revoked_at = EXCLUDED.revoked_at,
    updated_at = EXCLUDED.updated_at
WHERE EXCLUDED.updated_at > ai_consents.updated_at;

UPDATE ai_memories
SET memory_key = memory_type || '.legacy.' || memory_id
WHERE memory_key IS NULL OR memory_key = '';

UPDATE ai_memories
SET source_type = 'conversation'
WHERE source_type IS NULL OR source_type = '';

UPDATE ai_memories
SET sensitive = memory_type IN ('emotion_trend', 'conceptualization_clue')
WHERE sensitive IS NULL;

UPDATE ai_action_events
SET metadata = '{}'::jsonb,
    summary = CASE WHEN event_type = 'completed' THEN
        CASE module
            WHEN 'M1' THEN '完成情绪签到'
            WHEN 'M2' THEN '完成互动游戏'
            WHEN 'M3' THEN '完成舒缓练习'
            WHEN 'M5' THEN '完成社区互动'
            ELSE ''
        END
    ELSE '' END
WHERE summary IS NULL OR metadata <> '{}'::jsonb;

UPDATE ai_action_recommendations
SET feedback = CASE module
    WHEN 'M1' THEN '完成情绪签到'
    WHEN 'M2' THEN '完成互动游戏'
    WHEN 'M3' THEN '完成舒缓练习'
    WHEN 'M5' THEN '完成社区互动'
    ELSE ''
END
WHERE feedback <> '';

DELETE FROM ai_memories
WHERE memory_type = 'action_summary';

UPDATE ai_action_events
SET request_fingerprint = 'legacy:' || event_id
WHERE request_fingerprint IS NULL OR request_fingerprint = '';

ALTER TABLE ai_memories
    ALTER COLUMN source_type SET DEFAULT 'conversation',
    ALTER COLUMN source_type SET NOT NULL,
    ALTER COLUMN sensitive SET DEFAULT FALSE,
    ALTER COLUMN sensitive SET NOT NULL;

ALTER TABLE ai_action_events
    ALTER COLUMN summary SET DEFAULT '',
    ALTER COLUMN summary SET NOT NULL,
    ALTER COLUMN request_fingerprint SET NOT NULL;

DROP INDEX IF EXISTS ai_memories_current_key_idx;
CREATE UNIQUE INDEX ai_memories_current_key_idx
    ON ai_memories (user_id, memory_key)
    WHERE deleted_at IS NULL AND valid_until IS NULL AND memory_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ai_inspection_logs_subject_idx
    ON ai_inspection_logs (subject_hmac, created_at DESC)
    WHERE subject_hmac IS NOT NULL;

CREATE INDEX IF NOT EXISTS ai_memory_audit_user_idx
    ON ai_memory_audit (user_id);

CREATE INDEX IF NOT EXISTS ai_crisis_events_user_idx
    ON ai_crisis_events (user_id);

CREATE INDEX IF NOT EXISTS ai_action_events_user_idx
    ON ai_action_events (user_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS ai_privacy_deletion_pending_idx
    ON ai_privacy_deletion_audits (requested_at)
    WHERE status = 'redis_pending';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_memories_user_fk'
          AND conrelid = 'ai_memories'::regclass
    ) THEN
        ALTER TABLE ai_memories
            ADD CONSTRAINT ai_memories_user_fk FOREIGN KEY (user_id)
            REFERENCES ai_users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_memories_supersedes_fk'
          AND conrelid = 'ai_memories'::regclass
    ) THEN
        ALTER TABLE ai_memories
            ADD CONSTRAINT ai_memories_supersedes_fk FOREIGN KEY (supersedes_memory_id)
            REFERENCES ai_memories(memory_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_action_recommendations_user_fk'
          AND conrelid = 'ai_action_recommendations'::regclass
    ) THEN
        ALTER TABLE ai_action_recommendations
            ADD CONSTRAINT ai_action_recommendations_user_fk FOREIGN KEY (user_id)
            REFERENCES ai_users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_action_events_user_fk'
          AND conrelid = 'ai_action_events'::regclass
    ) THEN
        ALTER TABLE ai_action_events
            ADD CONSTRAINT ai_action_events_user_fk FOREIGN KEY (user_id)
            REFERENCES ai_users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_crisis_events_user_fk'
          AND conrelid = 'ai_crisis_events'::regclass
    ) THEN
        ALTER TABLE ai_crisis_events
            ADD CONSTRAINT ai_crisis_events_user_fk FOREIGN KEY (user_id)
            REFERENCES ai_users(user_id) ON DELETE CASCADE;
    END IF;
END
$$;

DROP TABLE ai_memory_consents;

REVOKE ALL ON ai_privacy_deletion_audits, ai_privacy_tombstones FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_privacy_deletion_audits, ai_privacy_tombstones TO xiaoliao;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_inspection_logs, ai_lessons, ai_prompt_patches TO xiaoliao;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_crisis_events TO xiaoliao;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_lesson_subjects TO xiaoliao;
