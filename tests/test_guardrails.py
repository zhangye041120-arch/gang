import pytest

from xiaoliao_agent.aging import apply_aging_filter
from xiaoliao_agent.guardrails import precheck, MEDICAL_DISCLAIMER


def test_crisis_is_detected():
    result = precheck("我不想活了")
    assert result.crisis_detected


def test_aging_filter_replaces_banned_phrases():
    reply = apply_aging_filter("你别多想，想开点，这很简单。")
    assert "想开点" not in reply
    assert "别多想" not in reply
    assert "这很简单" not in reply


@pytest.mark.parametrize("text", [
    "我活着就是拖累孩子",
    "我一觉睡过去别醒就好了",
    "没有我，他们过得更好",
    "我活够了",
])
def test_implicit_elder_crisis_phrases_are_detected(text):
    result = precheck(text)
    assert result.crisis_detected
    assert result.rule_ids


def test_medical_reply_appends_disclaimer_not_block():
    """Model reply with drug advice should trigger append_disclaimer, not hard block."""
    result = precheck("我最近不舒服", "你应该加大剂量吃药")
    assert result.risk_category == "medical_boundary"
    assert result.append_disclaimer is True
    assert result.safety_violation is True


def test_medical_input_alone_does_not_block():
    """User asking about medication should NOT be blocked before the model answers."""
    result = precheck("我该吃什么药")
    assert result.risk_category == "normal"
    assert not result.safety_violation


def test_ordinary_body_discomfort_is_not_treated_as_medical_request():
    result = precheck("我最近身体有点不舒服")
    assert not result.crisis_detected
    assert not result.safety_violation


def test_fraud_transfer_and_verification_code_phrases_are_flagged():
    assert precheck("请把钱转到这个账户").safety_violation
    assert precheck("把验证码告诉我").safety_violation
    assert precheck("我是公检法，需要你配合调查").safety_violation
    assert precheck("刷单返利，稳赚不赔").safety_violation


def test_fraud_rules_are_not_false_positive_on_normal_phrases():
    assert not precheck("我想给儿子转学").safety_violation
    assert not precheck("我记不住密码了").safety_violation


@pytest.mark.parametrize("text", [
    "我想设置停药提醒",
    "医生让我停药了",
    "我停药后需要注意什么",
    "我上个月停药了，最近感觉还好",
    "感冒一般吃什么药",
    "怎么诊断抑郁症",
    "得了抑郁症的人要怎么相处",
    "这个治疗方案是什么",
    "医生给我开了处方，让我按时吃",
    "医生说确诊我是高血压，让我按时吃药",
    "这个药是我老伴在吃，我想了解注意事项",
    # These used to be blocked on input; now the model gets to answer first.
    "我该吃什么药",
    "我应该吃多大剂量",
    "帮我开个处方",
    "我觉得我该加药",
    "这个药我能停吗",
    "我该不该停药",
    "你能不能帮我判断我是不是抑郁",
])
def test_medical_input_questions_are_not_blocked(text):
    """All medical questions from the user are allowed through to the model."""
    result = precheck(text)
    assert result.risk_category == "normal"
    assert not result.safety_violation


@pytest.mark.parametrize("reply", [
    "好的，已帮你记录停药提醒",
    "医生让你停药了，按医生要求观察即可",
    "停药后建议注意休息",
    "我不能替你做停药决定，请联系医生",
    "抑郁症怎么诊断要由专业医生评估",
    "医生说诊断为感冒，按医嘱休息即可",
])
def test_safe_medical_context_output_is_not_flagged(reply):
    result = precheck("普通消息", reply)
    assert result.risk_category == "normal"
    assert not result.safety_violation


@pytest.mark.parametrize("reply", [
    "你可以停药试试",
    "你应该加大剂量吃药",
    "你最好把药减了",
])
def test_personal_medical_advice_triggers_disclaimer(reply):
    """Model replies with personal drug/treatment advice get disclaimer appended."""
    result = precheck("普通消息", reply)
    assert result.risk_category == "medical_boundary"
    assert result.append_disclaimer is True
    assert result.safety_violation is True


def test_disclaimer_text_is_correct():
    assert "我不能替医生做诊断" in MEDICAL_DISCLAIMER
    assert "请联系专业医生" in MEDICAL_DISCLAIMER
