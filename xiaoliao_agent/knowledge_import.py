from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .knowledge import _source_name, split_markdown
from .knowledge_repository import KnowledgeRepository


@dataclass
class ImportStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0


def import_files(
    repository: KnowledgeRepository,
    paths: Iterable[Path],
    *,
    version: str = "v1",
) -> ImportStats:
    stats = ImportStats()
    for path in paths:
        try:
            source = _source_name(path.stem)
            chunks = split_markdown(path.read_text(encoding="utf-8"), source=source, version=version)
            for chunk in chunks:
                result = repository.upsert(chunk)
                if result == "inserted":
                    stats.inserted += 1
                elif result == "updated":
                    stats.updated += 1
                else:
                    stats.skipped += 1
        except Exception:
            stats.failed += 1
    return stats
