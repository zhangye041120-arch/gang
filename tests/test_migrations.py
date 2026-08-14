from pathlib import Path

import pytest

from xiaoliao_agent.migrations import (
    MigrationChecksumMismatch,
    discover_migrations,
    latest_migration_version,
    plan_migrations,
)


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="")


def test_migrations_are_numeric_unique_and_ordered(tmp_path):
    write(tmp_path / "002_second.sql", "SELECT 2;\n")
    write(tmp_path / "001_first.sql", "SELECT 1;\n")

    migrations = discover_migrations(tmp_path)

    assert [item.version for item in migrations] == ["001", "002"]
    assert [item.name for item in migrations] == ["001_first.sql", "002_second.sql"]


def test_invalid_or_duplicate_migration_versions_are_rejected(tmp_path):
    write(tmp_path / "001_first.sql", "SELECT 1;")
    write(tmp_path / "001_other.sql", "SELECT 2;")
    write(tmp_path / "bad.sql", "SELECT 3;")

    with pytest.raises(ValueError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_checksum_normalizes_line_endings(tmp_path):
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "001_first.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    (crlf / "001_first.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")

    assert discover_migrations(lf)[0].checksum == discover_migrations(crlf)[0].checksum


def test_plan_returns_only_unapplied_migrations(tmp_path):
    write(tmp_path / "001_first.sql", "SELECT 1;")
    write(tmp_path / "002_second.sql", "SELECT 2;")
    migrations = discover_migrations(tmp_path)

    pending = plan_migrations({"001": migrations[0].checksum}, migrations)

    assert [item.version for item in pending] == ["002"]


def test_checksum_change_is_rejected(tmp_path):
    write(tmp_path / "001_first.sql", "SELECT 1;")
    migration = discover_migrations(tmp_path)[0]

    with pytest.raises(MigrationChecksumMismatch) as exc_info:
        plan_migrations({migration.version: "wrong-checksum"}, [migration])

    assert migration.version in str(exc_info.value)
    assert migration.sql not in str(exc_info.value)


def test_latest_version_uses_repository_migrations():
    root = Path(__file__).resolve().parents[1] / "migrations"

    assert latest_migration_version(root) == "008"


def test_reminder_migration_has_durable_and_idempotent_constraints():
    root = Path(__file__).resolve().parents[1] / "migrations"
    sql = (root / "008_agent_reminders.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS ai_reminders" in sql
    assert "UNIQUE (user_id, fingerprint)" in sql
    assert "due_at" in sql
    assert "REVOKE ALL" in sql
