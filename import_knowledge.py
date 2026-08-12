import argparse
from pathlib import Path

from xiaoliao_agent.config import Settings
from xiaoliao_agent.knowledge_import import import_files
from xiaoliao_agent.knowledge_repository import MemoryKnowledgeRepository, PostgresKnowledgeRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="导入小辽 CBT 与教训知识库")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="删除数据库中不在当前文件来源的旧知识块",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    paths = args.paths or [
        settings.knowledge_path,
        settings.lessons_path,
    ]
    if settings.knowledge_database_url:
        repository = PostgresKnowledgeRepository(settings.knowledge_database_url)
        mode = "postgres"
    else:
        repository = MemoryKnowledgeRepository()
        mode = "memory-dry-run"
    stats = import_files(
        repository,
        paths,
        version=settings.knowledge_version,
        prune=args.prune,
    )
    print(
        f"mode={mode} inserted={stats.inserted} updated={stats.updated} "
        f"skipped={stats.skipped} pruned={stats.pruned} failed={stats.failed}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
