import json
import pytest

from xiaoliao_agent.agent import XiaoliaoAgent
from xiaoliao_agent.config import Settings
from xiaoliao_agent.live_context import LiveContext
from xiaoliao_agent.reminders import ReminderService


class FakeMainClient:
    calls = 0

    def chat(self, messages, *, json_mode=False):
        type(self).calls += 1
        return json.dumps({
            "reply": "听起来你最近有些累。愿不愿意先记一下今天的心情？",
            "intent": "emotion_support",
            "action": {
                "type": "miniprogram",
                "module": "M1",
                "page": "/pages/checkin/index",
                "params": {},
                "reason": "invite_small_action",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
            "risk_hint": "none",
        }, ensure_ascii=False)


class FakeInspectorClient:
    def chat(self, messages, *, json_mode=False):
        return json.dumps({
            "crisis_detected": False,
            "safety_violation": False,
            "intent_accurate": True,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": [],
            "suggestion": "",
            "error_pattern": "none",
            "lesson": "",
        }, ensure_ascii=False)


class ExplodingKnowledgeBase:
    def context(self, *args, **kwargs):
        raise AssertionError("危机输入不应进入知识检索")


class InvalidBoolInspectorClient:
    def chat(self, messages, *, json_mode=False):
        return json.dumps({
            "crisis_detected": "yes",
            "safety_violation": False,
            "intent_accurate": True,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": [],
            "suggestion": "",
            "error_pattern": "none",
            "lesson": "",
        }, ensure_ascii=False)


class RiskHintMainClient(FakeMainClient):
    def chat(self, messages, *, json_mode=False):
        data = json.loads(super().chat(messages, json_mode=json_mode))
        data["risk_hint"] = "medical_boundary"
        return json.dumps(data, ensure_ascii=False)


class SoftFailInspectorClient:
    def chat(self, messages, *, json_mode=False):
        return json.dumps({
            "crisis_detected": False,
            "safety_violation": False,
            "intent_accurate": False,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": ["intent 存疑"],
            "suggestion": "再确认意图",
            "error_pattern": "intent_mismatch",
            "lesson": "",
        }, ensure_ascii=False)


class CleanThinkingInspectorClient:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, json_mode=False):
        self.calls += 1
        return json.dumps({
            "crisis_detected": False,
            "safety_violation": False,
            "intent_accurate": True,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": [],
            "suggestion": "",
            "error_pattern": "none",
            "lesson": "",
        }, ensure_ascii=False)


class BlockingEscalationInspectorClient:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, json_mode=False):
        self.calls += 1
        return json.dumps({
            "crisis_detected": False,
            "safety_violation": True,
            "intent_accurate": True,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": ["医疗边界"],
            "suggestion": "",
            "error_pattern": "medical_boundary",
            "lesson": "",
        }, ensure_ascii=False)


def make_agent():
    FakeMainClient.calls = 0
    return XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
    )


def test_agent_pipeline_contract():
    result = make_agent().chat("我最近有点累")
    assert result.reply
    assert result.action["module"] == "M1"
    assert result.recommendation_id
    assert not result.blocked


def test_same_session_does_not_recommend_same_module_twice():
    agent = make_agent()
    first = agent.chat("我最近有点累", user_id="u1", session_id="s1")
    second = agent.chat("我最近有点累", user_id="u1", session_id="s1")
    assert first.action["module"] == "M1"
    assert second.action is None
    assert second.error_code is None
    other_session = agent.chat("我最近有点累", user_id="u1", session_id="s2")
    assert other_session.action["module"] == "M1"


def test_inspector_client_uses_thinking_off_by_default():
    settings = Settings(
        deepseek_api_key="main-key",
        qwen_api_key="inspector-key",
        inspector_enable_thinking=False,
    )
    assert settings.inspector_escalate_on_issues is True
    agent = XiaoliaoAgent(settings)
    assert agent.main_client.enable_thinking is None
    assert agent.inspector_client.enable_thinking is False


def test_inspector_escalation_confirms_soft_fail_without_unnecessary_rewrite():
    thinking = CleanThinkingInspectorClient()
    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=SoftFailInspectorClient(),
        inspector_escalation_client=thinking,
    )
    result = agent.chat("我最近有点累")
    assert thinking.calls == 1
    assert not result.blocked
    assert not result.rewritten


def test_inspector_escalation_blocks_when_thinking_pass_finds_safety_issue():
    thinking = BlockingEscalationInspectorClient()
    agent = XiaoliaoAgent(
        Settings(),
        main_client=RiskHintMainClient(),
        inspector_client=FakeInspectorClient(),
        inspector_escalation_client=thinking,
    )
    result = agent.chat("我最近有点累")
    assert thinking.calls == 1
    assert result.blocked
    assert result.safety_violation


def test_clean_fast_pass_does_not_create_escalation_client():
    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
    )
    result = agent.chat("我最近有点累")
    assert not result.blocked
    assert agent._inspector_escalation_client is None


def test_escalation_can_be_disabled_by_config():
    agent = XiaoliaoAgent(
        Settings(inspector_escalate_on_issues=False),
        main_client=RiskHintMainClient(),
        inspector_client=FakeInspectorClient(),
    )
    result = agent.chat("我最近有点累")
    assert agent._inspector_escalation_client is None
    assert not result.blocked


def test_chat_stream_escalates_when_fast_inspector_is_uncertain():
    thinking = BlockingEscalationInspectorClient()
    agent = XiaoliaoAgent(
        Settings(),
        main_client=RiskHintMainClient(),
        inspector_client=FakeInspectorClient(),
        inspector_escalation_client=thinking,
    )
    events = list(agent.chat_stream("我最近有点累"))
    done = [event for event in events if event.get("type") == "done"][0]
    assert thinking.calls == 1
    assert done["blocked"] is True


def test_fetch_rag_includes_live_clock_context_and_sources():
    live = LiveContext(
        label="live:clock",
        section="当前时间",
        content="现在是 2026年8月7日 星期五 14:30（北京时间）。",
        sources=[{"source": "live:clock", "title": "当前时间", "content": "现在是 2026年8月7日"}],
    )
    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
        live_context_provider=lambda text: live,
    )
    combined, _memory, sources = agent._fetch_rag("现在几点", "user", False, "")
    assert "[当前时间]" in combined
    assert "2026年8月7日" in combined
    assert any(item["source"] == "live:clock" for item in sources)


def test_crisis_input_never_triggers_live_lookup():
    calls = []

    def provider(text):
        calls.append(text)
        raise AssertionError("危机输入不应触发实时查询")

    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
        live_context_provider=provider,
    )
    result = agent.chat("我想自杀", user_id="u", session_id="s")
    assert calls == []
    assert result.crisis_detected
    assert result.blocked


def test_fetch_rag_records_reminder_and_injects_confirmation_context():
    service = ReminderService()
    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
        reminder_service=service,
    )
    combined, _memory, sources = agent._fetch_rag("每天上午8点提醒我吃药", "user-rem", False, "")
    assert "[提醒]" in combined
    assert "吃药" in combined
    assert any(item["source"] == "reminder:created" for item in sources)
    assert len(service.repository.list_for_user("user-rem")) == 1


def test_crisis_precheck_runs_before_delivery():
    result = make_agent().chat("我不想活了")
    assert result.blocked
    assert result.crisis_detected
    assert FakeMainClient.calls == 0


def test_crisis_precheck_runs_before_knowledge_retrieval():
    agent = make_agent()
    agent.kb = ExplodingKnowledgeBase()
    result = agent.chat("我已经准备好了")
    assert result.blocked
    assert result.crisis_detected


def test_medical_input_is_blocked_before_model_call():
    result = make_agent().chat("我是不是得了抑郁症，该吃什么药")
    assert result.blocked
    assert result.safety_violation
    assert FakeMainClient.calls == 0


def test_explicit_diagnosis_request_is_blocked_before_model_call():
    result = make_agent().chat("请帮我诊断一下我的失眠")
    assert result.blocked
    assert result.safety_violation
    assert FakeMainClient.calls == 0


def test_inspector_string_false_is_rejected_by_strict_contract():
    from xiaoliao_agent.agent import AgentInvalidInspectionError, parse_inspection

    with pytest.raises(AgentInvalidInspectionError):
        parse_inspection(json.dumps({
            "crisis_detected": "false",
            "safety_violation": "false",
            "intent_accurate": "false",
            "age_appropriate": "true",
            "cbt_appropriate": "true",
            "issues": [],
            "suggestion": "",
            "error_pattern": "none",
            "lesson": "",
        }))


def test_invalid_inspector_boolean_is_treated_as_unsafe():
    agent = XiaoliaoAgent(
        Settings(),
        main_client=FakeMainClient(),
        inspector_client=InvalidBoolInspectorClient(),
    )
    result = agent.chat("我最近有点累")
    assert result.blocked
    assert result.safety_violation


def test_chat_stream_emits_safe_final_reply_after_inspection():
    events = list(make_agent().chat_stream("我最近有点累"))
    tokens = [event for event in events if event.get("type") == "token"]
    done = [event for event in events if event.get("type") == "done"]
    assert tokens
    assert done
    assert done[0]["blocked"] is False
    assert all(len(event.get("content", "")) <= 4 for event in tokens)
    assert "".join(event.get("content", "") for event in tokens) == "听起来你最近有些累。愿不愿意先记一下今天的心情？"
