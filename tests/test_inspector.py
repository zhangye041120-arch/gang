import json

import pytest

from xiaoliao_agent.agent import AgentInvalidInspectionError, XiaoliaoAgent, parse_inspection
from xiaoliao_agent.client import ModelTimeoutError
from xiaoliao_agent.config import Settings
from xiaoliao_agent.guardrails import CRISIS_FALLBACK, GENERIC_FALLBACK, SAFETY_FALLBACK
from xiaoliao_agent.inspection_repository import MemoryLessonRepository


def main_payload(reply):
    return json.dumps({
        "reply": reply,
        "intent": "emotion_support",
        "action": None,
        "risk_hint": "none",
    }, ensure_ascii=False)


def inspection_payload(**overrides):
    payload = {
        "crisis_detected": False,
        "safety_violation": False,
        "intent_accurate": True,
        "age_appropriate": True,
        "cbt_appropriate": True,
        "issues": [],
        "suggestion": "",
        "error_pattern": "none",
        "lesson": "",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class EmptyKnowledgeBase:
    def context(self, query, top_k=3):
        return "", []


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_request_id = "req-inspection"

    def chat(self, messages, *, json_mode=False):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_agent(main_responses, inspection_responses, repository=None):
    main = SequenceClient(main_responses)
    inspector = SequenceClient(inspection_responses)
    agent = XiaoliaoAgent(
        Settings(),
        knowledge_base=EmptyKnowledgeBase(),
        main_client=main,
        inspector_client=inspector,
        lesson_repository=repository,
    )
    return agent, main, inspector


@pytest.mark.parametrize("payload", [
    {"crisis_detected": False},
    json.loads(inspection_payload(crisis_detected="false")),
    json.loads(inspection_payload(issues="not-a-list")),
    {**json.loads(inspection_payload()), "extra": True},
])
def test_inspection_rejects_missing_wrong_string_bool_and_extra_fields(payload):
    with pytest.raises(AgentInvalidInspectionError):
        parse_inspection(json.dumps(payload, ensure_ascii=False))


def test_unknown_error_pattern_is_normalized():
    result = parse_inspection(inspection_payload(error_pattern="model_invented_value"))
    assert result.error_pattern == "unknown"


@pytest.mark.parametrize(("inspection", "expected_reply", "crisis", "safety"), [
    (inspection_payload(crisis_detected=True, error_pattern="crisis"), CRISIS_FALLBACK, True, False),
    (inspection_payload(safety_violation=True, error_pattern="medical_boundary"), SAFETY_FALLBACK, False, True),
])
def test_hard_inspection_failure_never_sends_candidate(inspection, expected_reply, crisis, safety):
    agent, main, _ = make_agent([main_payload("未经审核候选")], [inspection])
    result = agent.chat("我最近有点累")
    assert result.reply == expected_reply
    assert result.reply != "未经审核候选"
    assert result.crisis_detected is crisis
    assert result.safety_violation is safety
    assert main.calls == 1


def test_soft_failure_rewrites_once_and_reinspects():
    first_soft = inspection_payload(intent_accurate=False, suggestion="先共情", error_pattern="intent_mismatch")
    agent, main, inspector = make_agent(
        [main_payload("第一版"), main_payload("通过的重写")],
        [first_soft, inspection_payload()],
    )
    result = agent.chat("我最近有点累")
    assert result.reply == "通过的重写"
    assert result.rewritten
    assert main.calls == 2
    assert inspector.calls == 2


def test_second_soft_failure_uses_conservative_fallback_without_third_rewrite():
    soft = inspection_payload(age_appropriate=False, suggestion="改短", error_pattern="age_inappropriate")
    agent, main, inspector = make_agent(
        [main_payload("第一版"), main_payload("仍未通过的重写")],
        [soft, soft],
    )
    result = agent.chat("我最近有点累")
    assert result.reply == GENERIC_FALLBACK
    assert result.reply != "仍未通过的重写"
    assert result.error_code == "AGENT_INSPECTION_FAILED"
    assert main.calls == 2
    assert inspector.calls == 2


@pytest.mark.parametrize("bad_inspection", [
    "not-json",
    inspection_payload(crisis_detected="false"),
    ModelTimeoutError("timeout", request_id="req-inspector-timeout"),
])
def test_invalid_or_failed_inspector_is_treated_as_unsafe(bad_inspection):
    agent, main, _ = make_agent([main_payload("未经审核候选")], [bad_inspection])
    result = agent.chat("我最近有点累")
    assert result.reply == SAFETY_FALLBACK
    assert result.safety_violation
    assert result.reply != "未经审核候选"
    assert main.calls == 1


def test_lesson_is_pending_until_reviewed_with_reason_and_reviewer():
    repository = MemoryLessonRepository()
    agent, _, _ = make_agent(
        [main_payload("候选")],
        [inspection_payload(lesson="先共情再提问", error_pattern="none")],
        repository,
    )
    agent.chat("我最近有点累")
    candidate = repository.list_candidates()[0]
    assert candidate.status == "pending"
    assert candidate.content == "先共情再提问"
    repository.review(candidate.candidate_id, "approved", reviewer="reviewer-1", reason="人工确认")
    reviewed = repository.get(candidate.candidate_id)
    assert reviewed.status == "approved"
    assert reviewed.reviewer == "reviewer-1"
    assert reviewed.reason == "人工确认"
