from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

try:
    from scripts.backup_database import BACKUP_NAME, pg_environment
except ModuleNotFoundError:  # Direct `python scripts/restore_database.py` execution.
    from backup_database import BACKUP_NAME, pg_environment


DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}$")


def validate_restore(
    backup: Path,
    target_database: str,
    source_database: str,
) -> Path:
    resolved = backup.expanduser().resolve()
    if not resolved.is_file() or not BACKUP_NAME.fullmatch(resolved.name):
        raise ValueError("backup must be an existing xiaoliao encrypted backup")
    if not DATABASE_NAME.fullmatch(target_database):
        raise ValueError("target database name is invalid")
    if target_database == source_database:
        raise ValueError("restore target must differ from the source database")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore into a disposable PostgreSQL database")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--database-url-env", default="KNOWLEDGE_DATABASE_URL")
    parser.add_argument("--age-identity-env", default="AGE_IDENTITY_FILE")
    args = parser.parse_args()
    if args.confirm_target != args.target_database:
        raise ValueError("confirm-target must exactly match target-database")
    database_url = os.getenv(args.database_url_env, "")
    identity_file = os.getenv(args.age_identity_env, "")
    if not database_url or not identity_file:
        raise RuntimeError("database URL and age identity environment variables are required")
    source_database = urlsplit(database_url).path.lstrip("/")
    backup = validate_restore(args.backup, args.target_database, source_database)
    if shutil.which("age") is None or shutil.which("pg_restore") is None:
        raise RuntimeError("age and pg_restore are required")

    plain_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as temporary:
            plain_path = Path(temporary.name)
        subprocess.run(
            [
                "age", "--decrypt", "--identity", identity_file,
                "--output", str(plain_path), str(backup),
            ],
            check=True,
        )
        subprocess.run(
            [
                "pg_restore", "--clean", "--if-exists", "--no-owner",
                "--exit-on-error", "--dbname", args.target_database, str(plain_path),
            ],
            check=True,
            env=pg_environment(database_url, target_database=args.target_database),
        )
    finally:
        if plain_path is not None:
            plain_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
