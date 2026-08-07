import argparse
from pathlib import Path

from xiaoliao_agent.config import Settings
from xiaoliao_agent.knowledge_import import import_files
from xiaoliao_agent.knowledge_repository import MemoryKnowledgeRepository, PostgresKnowledgeRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="导入小辽 CBT 与教训知识库")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    paths = args.paths or [
        settings.knowledge_path,
        settings.lessons_path,
        settings.elder_scenarios_path,
        settings.regional_resources_path,
        settings.health_knowledge_path,
        settings.fraud_knowledge_path,
        settings.leisure_knowledge_path,
    ]
    if settings.knowledge_database_url:
        repository = PostgresKnowledgeRepository(settings.knowledge_database_url)
        mode = "postgres"
    else:
        repository = MemoryKnowledgeRepository()
        mode = "memory-dry-run"
    stats = import_files(repository, paths, version=settings.knowledge_version)
    print(
        f"mode={mode} inserted={stats.inserted} updated={stats.updated} "
        f"skipped={stats.skipped} failed={stats.failed}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
