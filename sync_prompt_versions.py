import hashlib
import json

import psycopg

from xiaoliao_agent.config import Settings
from xiaoliao_agent.prompts import PROMPT_ROOT


def main() -> int:
    settings = Settings.from_env()
    registry = json.loads((PROMPT_ROOT / "registry.json").read_text(encoding="utf-8"))
    with psycopg.connect(settings.knowledge_database_url) as connection:
        for item in registry["prompts"]:
            content = (PROMPT_ROOT / item["content_file"]).read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO ai_prompt_versions
                    (prompt_id, semantic_version, content_hash, owner, changelog, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (prompt_id, semantic_version) DO NOTHING
                """,
                (item["prompt_id"], item["semantic_version"], digest, item["owner"], item["changelog"], item["created_at"]),
            )
            stored = connection.execute(
                "SELECT content_hash FROM ai_prompt_versions WHERE prompt_id=%s AND semantic_version=%s",
                (item["prompt_id"], item["semantic_version"]),
            ).fetchone()[0]
            if stored != digest:
                raise RuntimeError(f"不可变 Prompt 版本哈希冲突：{item['prompt_id']}@{item['semantic_version']}")
    print(f"synced={len(registry['prompts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
