from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompt_versions"


class PromptVersionError(ValueError):
    pass


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    semantic_version: str
    content: str
    changelog: str
    owner: str
    scope: str
    created_at: str
    rollback_to: str | None


def _catalog() -> dict[str, Any]:
    try:
        return json.loads((PROMPT_ROOT / "registry.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptVersionError("Prompt 版本目录不可用") from exc


def _entry(prompt_id: str, version: str | None) -> dict[str, Any]:
    catalog = _catalog()
    selected = version or catalog.get("default_versions", {}).get(prompt_id)
    for item in catalog.get("prompts", []):
        if item.get("prompt_id") == prompt_id and item.get("semantic_version") == selected:
            return item
    raise PromptVersionError(f"未找到 Prompt 版本：{prompt_id}@{selected}")


def get_prompt_spec(prompt_id: str, version: str | None = None) -> PromptSpec:
    item = _entry(prompt_id, version)
    content_name = item.get("content_file", "")
    content_path = (PROMPT_ROOT / content_name).resolve()
    if PROMPT_ROOT.resolve() not in content_path.parents:
        raise PromptVersionError("Prompt 内容路径越界")
    try:
        content = content_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptVersionError(f"Prompt 内容不可用：{prompt_id}") from exc
    return PromptSpec(
        prompt_id=item["prompt_id"],
        semantic_version=item["semantic_version"],
        content=content,
        changelog=item["changelog"],
        owner=item["owner"],
        scope=item["scope"],
        created_at=item["created_at"],
        rollback_to=item.get("rollback_to"),
    )


def list_prompt_specs() -> list[PromptSpec]:
    catalog = _catalog()
    return [get_prompt_spec(item["prompt_id"], item["semantic_version"]) for item in catalog.get("prompts", [])]


def select_prompt_version(prompt_id: str, version: str, *, content: str | None = None) -> PromptSpec:
    if content is not None:
        raise PromptVersionError("Prompt 版本不可覆盖；请新增语义版本文件")
    return get_prompt_spec(prompt_id, version)


def get_fallback_reply(version: str) -> str:
    try:
        fallbacks = json.loads((PROMPT_ROOT / "fallbacks.json").read_text(encoding="utf-8"))
        return fallbacks[version]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PromptVersionError(f"未找到降级话术版本：{version}") from exc
