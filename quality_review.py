import psycopg

from xiaoliao_agent.config import Settings


def main() -> int:
    settings = Settings.from_env()
    with psycopg.connect(settings.knowledge_database_url) as connection:
        patterns = connection.execute(
            "SELECT error_pattern, count(*) FROM ai_inspection_logs GROUP BY error_pattern ORDER BY count(*) DESC"
        ).fetchall()
        pending_lessons = connection.execute("SELECT count(*) FROM ai_lessons WHERE status='pending'").fetchone()[0]
        pending_patches = connection.execute("SELECT count(*) FROM ai_prompt_patches WHERE status='pending'").fetchone()[0]
        crisis_index = connection.execute(
            "SELECT event_id, evidence_code, route, status, created_at FROM ai_crisis_events ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    print("error_patterns")
    for pattern, count in patterns:
        print(f"{pattern}: {count}")
    print(f"pending_lessons={pending_lessons}")
    print(f"pending_patches={pending_patches}")
    print(f"crisis_case_index={len(crisis_index)}")
    for event_id, evidence_code, route, status, created_at in crisis_index:
        print(f"{event_id}|{evidence_code}|{route}|{status}|{created_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
