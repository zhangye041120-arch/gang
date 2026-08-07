from xiaoliao_agent.guardrails import precheck


def test_crisis_is_detected():
    result = precheck("我不想活了")
    assert result.crisis_detected


def test_medical_reply_is_blocked():
    result = precheck("我最近不舒服", "你应该加大剂量吃药")
    assert result.safety_violation


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
