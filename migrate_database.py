import argparse
import sys

from xiaoliao_agent.config import Settings
from xiaoliao_agent.migrations import apply_migrations


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply ordered M7 database migrations")
    parser.add_argument("--database-url", default="")
    args = parser.parse_args()
    database_url = args.database_url.strip() or Settings.from_env().knowledge_database_url
    if not database_url:
        print("KNOWLEDGE_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        applied = apply_migrations(database_url)
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("applied migrations: " + (", ".join(applied) if applied else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
