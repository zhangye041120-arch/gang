import json

import pytest

from xiaoliao_agent.agent import XiaoliaoAgent
from xiaoliao_agent.config import Settings
from xiaoliao_agent.quality import MemoryQualityRepository, QualityService


def main_reply(text="候选回复"):
    return json.dumps({"reply": text, "intent": "chat", "action": None, "risk_hint": "none"}, ensure_ascii=False)


def inspection(**overrides):
    data = {
        "crisis_detected": False, "safety_violation": False, "intent_accurate": True,
        "age_appropriate": True, "cbt_appropriate": True, "issues": [], "suggestion": "",
        "error_pattern": "none", "lesson": "",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.last_request_id = None

    def chat(self, messages, *, json_mode=False):
        self.last_request_id = self.last_request_id or "request-from-main"
        return self.responses.pop(0)


class EmptyKnowledge:
    def context(self, query, top_k=3):
        return "", []


def run_case(message, main_responses, inspector_responses, *, fail_logs=False):
    repository = MemoryQualityRepository(fail_writes=fail_logs)
    agent = XiaoliaoAgent(
        Settings(), knowledge_base=EmptyKnowledge(), main_client=SequenceClient(main_responses),
        inspector_client=SequenceClient(inspector_responses), quality_service=QualityService(repository),
    )
    result = agent.chat(message, user_id="real-user-must-not-be-logged", message_id="msg-test")
    return result, repository, agent.quality_service


@pytest.mark.parametrize(("message", "main_responses", "inspector_responses"), [
    ("普通消息", [main_reply()], [inspection()]),
    ("普通消息", ["not-json", "not-json"], []),
    ("普通消息", [main_reply()], ["not-json"]),
    ("我不想活了", [], []),
    ("普通消息", [main_reply("第一版"), main_reply("重写版")], [inspection(intent_accurate=False, error_pattern="intent_mismatch"), inspection()]),
])
def test_agent_terminal_paths_write_exactly_one_privacy_safe_log(message, main_responses, inspector_responses):
    result, repository, _ = run_case(message, main_responses, inspector_responses)
    assert len(repository.logs) == 1
    item = next(iter(repository.logs.values()))
    assert item.request_id == result.request_id
    assert item.message_id == "msg-test"
    assert item.user_hash != "real-user-must-not-be-logged"
    assert "real-user" not in repr(item)
    assert item.candidate_reply_ref.startswith("hmac-sha256:")
    assert item.subject_hmac == item.user_hash


def test_agent_log_failure_returns_reply_and_structured_alert():
    result, repository, service = run_case("普通消息", [main_reply()], [inspection()], fail_logs=True)
    assert result.reply
    assert "inspection_log_write_failed" in result.alerts
    assert len(service.retry_queue) == 1
    assert repository.logs == {}
