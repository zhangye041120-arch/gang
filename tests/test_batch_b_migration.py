from pathlib import Path
import os
import shutil
import uuid

import pytest

from xiaoliao_agent.migrations import apply_migrations, latest_migration_version


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "009_gateway_memory_privacy.sql"
MIGRATION_DATABASE_URL = os.getenv("MIGRATION_TEST_DATABASE_URL", "")


def migration_sql():
    return MIGRATION.read_text(encoding="utf-8")


def test_batch_b_owns_schema_version_nine():
    assert latest_migration_version(ROOT / "migrations") == "009"


def test_memory_versioning_and_sensitive_columns_are_migrated():
    sql = migration_sql()

    for column in (
        "memory_key",
        "supersedes_memory_id",
        "source_type",
        "sensitive",
    ):
        assert column in sql
    assert "ai_memories_current_key_idx" in sql


def test_action_and_quality_rows_gain_idempotency_and_subject_fields():
    sql = migration_sql()

    assert "request_fingerprint" in sql
    assert "summary" in sql
    assert "subject_hmac" in sql


def test_consent_is_reconciled_into_single_source_of_truth():
    sql = migration_sql()

    assert "INSERT INTO ai_consents" in sql
    assert "ai_memory_consents" in sql
    assert "DROP TABLE ai_memory_consents" in sql


def test_privacy_audit_and_tombstone_never_store_raw_user_id():
    sql = migration_sql()
    audit_block = sql.split("CREATE TABLE IF NOT EXISTS ai_privacy_deletion_audits", 1)[1]
    audit_block = audit_block.split(";", 1)[0]
    tombstone_block = sql.split("CREATE TABLE IF NOT EXISTS ai_privacy_tombstones", 1)[1]
    tombstone_block = tombstone_block.split(";", 1)[0]

    assert "subject_hmac" in audit_block
    assert "user_id" not in audit_block
    assert "subject_hmac" in tombstone_block
    assert "user_id" not in tombstone_block


def test_existing_subject_rows_are_backfilled_before_foreign_keys():
    sql = migration_sql()
    backfill_position = sql.index("INSERT INTO ai_users")
    constraint_position = sql.index("ai_memories_user_fk")

    assert backfill_position < constraint_position
    assert "ON DELETE CASCADE" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in sql


def test_user_backfill_casts_nullable_birth_year_to_integer():
    sql = migration_sql()
    backfill = sql.split("INSERT INTO ai_users", 1)[1].split(";", 1)[0]

    assert "NULL::INTEGER" in backfill


def test_legacy_action_text_is_removed_during_upgrade():
    sql = migration_sql()

    assert "UPDATE ai_action_events" in sql
    assert "metadata = '{}'::jsonb" in sql
    assert "DELETE FROM ai_memories" in sql
    assert "memory_type = 'action_summary'" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_crisis_events" in sql


@pytest.mark.skipif(
    not MIGRATION_DATABASE_URL,
    reason="MIGRATION_TEST_DATABASE_URL is not configured",
)
def test_real_v8_upgrade_reconciles_and_minimizes_legacy_data(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    schema = "m7_upgrade_" + uuid.uuid4().hex
    separator = "&" if "?" in MIGRATION_DATABASE_URL else "?"
    schema_url = (
        MIGRATION_DATABASE_URL
        + separator
        + "options=-csearch_path%3D"
        + schema
        + "%2Cpublic"
    )
    v8_root = tmp_path / "migrations"
    v8_root.mkdir()
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        if path.name < "009_":
            shutil.copy2(path, v8_root / path.name)

    with psycopg.connect(MIGRATION_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    try:
        assert apply_migrations(schema_url, v8_root) == [
            f"{version:03d}" for version in range(1, 9)
        ]
        with psycopg.connect(schema_url) as connection:
            connection.execute(
                "INSERT INTO ai_users VALUES ('legacy-user','',NULL,'active',now(),now())"
            )
            connection.execute(
                "INSERT INTO ai_consents VALUES ('legacy-user',FALSE,FALSE,'v1',NULL,now(),now() - interval '1 day')"
            )
            connection.execute(
                "INSERT INTO ai_memory_consents VALUES ('legacy-user',TRUE,TRUE,now())"
            )
            connection.execute(
                """
                INSERT INTO ai_action_recommendations
                    (recommendation_id,user_id,session_id,module,action_json,reason,status,
                     source_message_id,expires_at,created_at,feedback)
                VALUES ('legacy-rec','legacy-user','s','M5','{}','reason','completed',
                        'src',NULL,now(),'legacy post body')
                """
            )
            connection.execute(
                """
                INSERT INTO ai_action_events
                    (event_id,recommendation_id,user_id,module,event_type,occurred_at,metadata)
                VALUES ('legacy-event','legacy-rec','legacy-user','M5','completed',now(),
                        '{"effort":"legacy post body"}')
                """
            )
            connection.execute(
                """
                INSERT INTO ai_memories
                    (memory_id,user_id,memory_type,content,content_hash,confidence,
                     source_message_id,consent_scope,valid_from,valid_until,created_at,
                     updated_at,deleted_at)
                VALUES ('legacy-memory','legacy-user','action_summary','legacy post body',
                        'legacy-hash',1.0,'legacy-event','personalization',now(),NULL,
                        now(),now(),NULL)
                """
            )

        assert apply_migrations(schema_url, ROOT / "migrations") == ["009"]
        with psycopg.connect(schema_url) as connection:
            assert connection.execute(
                "SELECT personalization,sensitive FROM ai_consents WHERE user_id='legacy-user'"
            ).fetchone() == (True, True)
            metadata, summary = connection.execute(
                "SELECT metadata,summary FROM ai_action_events WHERE event_id='legacy-event'"
            ).fetchone()
            assert metadata == {}
            assert summary == "完成社区互动"
            assert connection.execute(
                "SELECT feedback FROM ai_action_recommendations WHERE recommendation_id='legacy-rec'"
            ).fetchone()[0] == "完成社区互动"
            assert connection.execute(
                "SELECT count(*) FROM ai_memories WHERE memory_type='action_summary'"
            ).fetchone()[0] == 0
            connection.execute("DELETE FROM ai_users WHERE user_id='legacy-user'")
            assert connection.execute(
                "SELECT count(*) FROM ai_action_recommendations WHERE user_id='legacy-user'"
            ).fetchone()[0] == 0
        assert apply_migrations(schema_url, ROOT / "migrations") == []
    finally:
        with psycopg.connect(MIGRATION_DATABASE_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
