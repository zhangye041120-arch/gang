from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re


MIGRATION_ROOT = Path(__file__).resolve().parents[1] / "migrations"
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3})_[A-Za-z0-9_]+\.sql$")
_ADVISORY_LOCK_ID = 8_140_2026


class MigrationChecksumMismatch(RuntimeError):
    pass


class SchemaNotCurrent(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str
    sql: str


@dataclass(frozen=True)
class SchemaStatus:
    current_version: str | None
    expected_version: str
    pending_versions: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return not self.pending_versions and self.current_version == self.expected_version


def _normalized_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def discover_migrations(root: Path = MIGRATION_ROOT) -> list[Migration]:
    migrations: list[Migration] = []
    versions: set[str] = set()
    for path in sorted(root.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")
        version = match.group("version")
        if version in versions:
            raise ValueError(f"duplicate migration version: {version}")
        versions.add(version)
        sql = _normalized_sql(path)
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(Migration(version, path.name, path, checksum, sql))
    return sorted(migrations, key=lambda item: item.version)


def latest_migration_version(root: Path = MIGRATION_ROOT) -> str:
    migrations = discover_migrations(root)
    if not migrations:
        raise ValueError("no migrations found")
    return migrations[-1].version


def plan_migrations(
    applied: dict[str, str],
    migrations: list[Migration],
) -> list[Migration]:
    known_versions = {item.version for item in migrations}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise SchemaNotCurrent("database contains unknown migration versions: " + ", ".join(unknown))
    pending: list[Migration] = []
    for migration in migrations:
        applied_checksum = applied.get(migration.version)
        if applied_checksum is None:
            pending.append(migration)
        elif applied_checksum != migration.checksum:
            raise MigrationChecksumMismatch(
                f"migration {migration.version} checksum does not match the applied version"
            )
    return pending


def _ensure_ledger(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _applied_migrations(connection) -> dict[str, str]:
    rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {str(version): str(checksum) for version, checksum in rows}


def apply_migrations(
    database_url: str,
    root: Path = MIGRATION_ROOT,
) -> list[str]:
    if not database_url.strip():
        raise ValueError("database URL is required")
    import psycopg

    migrations = discover_migrations(root)
    applied_versions: list[str] = []
    with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as connection:
        _ensure_ledger(connection)
        connection.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_ID,))
        try:
            pending = plan_migrations(_applied_migrations(connection), migrations)
            for migration in pending:
                with connection.transaction():
                    connection.execute(migration.sql, prepare=False)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                applied_versions.append(migration.version)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_ID,))
    return applied_versions


def schema_status(
    database_url: str,
    root: Path = MIGRATION_ROOT,
) -> SchemaStatus:
    if not database_url.strip():
        raise ValueError("database URL is required")
    import psycopg

    migrations = discover_migrations(root)
    expected = migrations[-1].version
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        ledger_exists = connection.execute(
            "SELECT to_regclass('public.schema_migrations')"
        ).fetchone()[0]
        applied = _applied_migrations(connection) if ledger_exists else {}
    pending = plan_migrations(applied, migrations)
    current = max(applied) if applied else None
    return SchemaStatus(current, expected, tuple(item.version for item in pending))


def verify_schema(
    database_url: str,
    expected_version: str | None = None,
    root: Path = MIGRATION_ROOT,
) -> SchemaStatus:
    status = schema_status(database_url, root)
    expected = expected_version or latest_migration_version(root)
    if not status.is_current or status.expected_version != expected:
        raise SchemaNotCurrent(
            f"database schema is not current; expected migration {expected}"
        )
    return status
