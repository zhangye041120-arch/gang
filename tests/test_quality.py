from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from xiaoliao_agent.quality import (
    InspectionLog,
    MemoryQualityRepository,
    PromptPatchStore,
    QualityService,
)


def log(request_id="req-1", **overrides):
    data = {
        "request_id": request_id,
        "message_id": "msg-1",
        "user_hash": "hash-only",
        "candidate_reply_ref": "sha256:reply",
        "crisis_detected": False,
        "safety_violation": False,
        "intent_accurate": True,
        "age_appropriate": True,
        "cbt_appropriate": True,
        "issues": [],
        "latency_ms": 10,
        "main_model": "deepseek-v4-flash",
        "inspector_model": "qwen3.7-flash",
        "prompt_version": "1.2.0",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost": None,
        "error_pattern": "none",
        "lesson_ref": None,
    }
    data.update(overrides)
    return InspectionLog(**data)


def test_success_model_failure_inspector_failure_crisis_and_rewrite_are_loggable():
    repository = MemoryQualityRepository()
    service = QualityService(repository)
    cases = [
        log("success"),
        log("main-failure", error_pattern="invalid_response"),
        log("inspector-failure", safety_violation=True, error_pattern="invalid_response"),
        log("crisis", crisis_detected=True, error_pattern="crisis"),
        log("rewrite", error_pattern="intent_mismatch"),
    ]
    for item in cases:
        assert service.write_log(item) == []
    assert len(repository.logs) == 5


def test_log_failure_does_not_raise_and_retry_is_idempotent():
    repository = MemoryQualityRepository(fail_writes=True)
    service = QualityService(repository)
    alerts = service.write_log(log())
    assert alerts == ["inspection_log_write_failed"]
    assert len(service.retry_queue) == 1
    service.write_log(log())
    assert len(service.retry_queue) == 1
    repository.fail_writes = False
    service.retry_failed()
    assert len(repository.logs) == 1
    assert service.retry_queue == []


def test_concurrent_duplicate_request_id_writes_once():
    repository = MemoryQualityRepository()
    service = QualityService(repository)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: service.write_log(log("same-request")), range(10)))
    assert len(repository.logs) == 1


def test_lesson_hash_dedup_and_approved_only_retrieval():
    repository = MemoryQualityRepository()
    first = repository.add_lesson("先共情再提问", "intent_mismatch", "1.2.0")
    second = repository.add_lesson("先共情再提问", "intent_mismatch", "1.2.0")
    assert first.lesson_id == second.lesson_id
    assert repository.approved_lessons() == []
    repository.review_lesson(first.lesson_id, "approved", reviewer="reviewer", reason="人工确认")
    assert repository.approved_lessons()[0].content == "先共情再提问"


def test_prompt_patch_requires_full_approval_and_never_overwrites_old_prompt():
    store = PromptPatchStore()
    old_path = Path(__file__).parents[1] / "prompt_versions" / "main-agent-1.2.0.txt"
    old_content = old_path.read_text(encoding="utf-8")
    patch = store.create("intent_mismatch", "增加先共情约束")
    assert patch.status == "pending"
    with pytest.raises(ValueError):
        store.review(patch.patch_id, "approved", reviewer="", reason="", test_report="", target_version="")
    reviewed = store.review(
        patch.patch_id,
        "approved",
        reviewer="reviewer",
        reason="回归通过",
        test_report="report-001",
        target_version="1.3.0",
    )
    assert reviewed.status == "approved"
    assert old_path.read_text(encoding="utf-8") == old_content
