from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit


BACKUP_NAME = re.compile(r"^xiaoliao-\d{8}T\d{6}Z\.dump\.age$")


def validate_destination(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    forbidden = {Path.cwd().resolve(), Path.home().resolve(), Path(resolved.anchor)}
    if resolved in forbidden or not resolved.is_dir():
        raise ValueError("backup destination must be an existing dedicated directory")
    return resolved


def pg_environment(database_url: str, *, target_database: str | None = None) -> dict[str, str]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("database URL must be PostgreSQL")
    database = target_database or parsed.path.lstrip("/")
    if not database:
        raise ValueError("database name is required")
    environment = os.environ.copy()
    environment.update({
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": (
            unquote(parsed.password)
            if parsed.password
            else environment.get("PGPASSWORD", "")
        ),
        "PGDATABASE": database,
    })
    return environment


def prune_backups(destination: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for candidate in destination.iterdir():
        if not candidate.is_file() or not BACKUP_NAME.fullmatch(candidate.name):
            continue
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            candidate.unlink()
            removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an encrypted PostgreSQL backup")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--database-url-env", default="KNOWLEDGE_DATABASE_URL")
    parser.add_argument("--age-recipient", required=True)
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.retention_days <= 3650:
        raise ValueError("retention days must be between 1 and 3650")
    destination = validate_destination(args.destination)
    database_url = os.getenv(args.database_url_env, "")
    if not database_url:
        raise RuntimeError(f"{args.database_url_env} is required")
    if shutil.which("pg_dump") is None or shutil.which("age") is None:
        raise RuntimeError("pg_dump and age are required")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination / f"xiaoliao-{stamp}.dump.age"
    plain_path: Path | None = None
    encrypted_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination, prefix=".pending-", suffix=".dump", delete=False
        ) as temporary:
            plain_path = Path(temporary.name)
        encrypted_path = plain_path.with_suffix(".dump.age")
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-password", "--file", str(plain_path)],
            check=True,
            env=pg_environment(database_url),
        )
        subprocess.run(
            [
                "age", "--recipient", args.age_recipient,
                "--output", str(encrypted_path), str(plain_path),
            ],
            check=True,
        )
        os.chmod(encrypted_path, 0o600)
        os.replace(encrypted_path, final_path)
        prune_backups(destination, args.retention_days)
    finally:
        if plain_path is not None:
            plain_path.unlink(missing_ok=True)
        if encrypted_path is not None:
            encrypted_path.unlink(missing_ok=True)
    print(final_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
