from dataclasses import dataclass
import re
from typing import Literal


RiskCategory = Literal["crisis", "medical_boundary", "unsafe_content", "normal"]


@dataclass(frozen=True)
class RiskRule:
    rule_id: str
    pattern: str


CRISIS_RULES = (
    RiskRule("C001", r"不想活|活着没意思|活着没有意义|没有活下去的必要"),
    RiskRule("C002", r"想死|自杀|结束(?:自己)?生命|告别这个世界|去死"),
    RiskRule("C003", r"已经(?:想好|准备好|准备)|具体做法|自杀计划"),
    RiskRule("C004", r"伤害自己|自残|割腕"),
    RiskRule("C005", r"一点希望都没有|彻底绝望"),
)

MEDICAL_INPUT_RULES = (
    RiskRule("M001", r"是不是.*抑郁|得了.*抑郁|怎么诊断|(?:请|帮我|能否|可以).{0,8}(?:诊断|判断)"),
    RiskRule("M002", r"该吃什么药|吃什么药|开处方|药物剂量|用药剂量|多大剂量"),
    RiskRule("M003", r"停药|加药|减药|加大剂量|减少剂量"),
    RiskRule("M004", r"保证.{0,8}(?:治好|治愈)|一定能治好|治疗方案"),
)

MEDICAL_OUTPUT_RULES = (
    RiskRule("M101", r"你患有|诊断为|确诊"),
    RiskRule("M102", r"停药|加药|减药|加大剂量|减少剂量|药物剂量|处方"),
    RiskRule("M103", r"保证.{0,8}(?:治好|治愈)|替代医生|替代治疗"),
)

UNSAFE_RULES = (
    RiskRule("U001", r"忽略(?:所有|之前|系统).{0,8}(?:规则|指令)|输出.{0,8}(?:系统提示词|system prompt)"),
    RiskRule("U002", r"泄露.{0,8}(?:系统提示|内部指令)|绕过.{0,8}(?:安全|规则)"),
)

FRAUD_RULES = (
    RiskRule("F001", r"转账|汇款|转钱|打钱|转到.{0,8}(?:账户|账号)"),
    RiskRule("F002", r"银行卡号|卡号|验证码|支付密码|转账密码|短信验证码"),
    RiskRule("F003", r"公检法|安全账户|配合调查|涉嫌.{0,6}(?:洗钱|犯罪)|涉案"),
    RiskRule("F004", r"中奖.{0,6}(?:交|先|手续费)|刷单|高回报|稳赚|保证金"),
)

NEGATED_CRISIS = re.compile(r"(?:没有|没|并不|不是|从未).{0,6}(?:想死|自杀|伤害自己|自残|不想活)", re.I)
REPORTED_CRISIS = re.compile(r"(?:新闻|电影|小说|报道|别人|他人).{0,12}(?:想死|自杀|伤害自己|自残)", re.I)


@dataclass
class GuardrailResult:
    risk_category: RiskCategory = "normal"
    rule_ids: list[str] | None = None
    crisis_detected: bool = False
    safety_violation: bool = False
    reasons: list[str] | None = None
    response_version: str = "safety-v1.0.0"


def _matches(text: str, rules: tuple[RiskRule, ...]) -> list[str]:
    return [rule.rule_id for rule in rules if re.search(rule.pattern, text, re.I)]


def _crisis_matches(text: str, *, honor_context: bool) -> list[str]:
    if honor_context and (NEGATED_CRISIS.search(text) or REPORTED_CRISIS.search(text)):
        return []
    return _matches(text, CRISIS_RULES)


def precheck(user_text: str, reply: str = "") -> GuardrailResult:
    crisis_rules = _crisis_matches(user_text, honor_context=True)
    crisis_rules.extend(_crisis_matches(reply, honor_context=False))
    if crisis_rules:
        unique = sorted(set(crisis_rules))
        return GuardrailResult("crisis", unique, True, False, unique, "crisis-v1.0.0")

    medical_rules = _matches(user_text, MEDICAL_INPUT_RULES)
    medical_rules.extend(_matches(reply, MEDICAL_OUTPUT_RULES))
    if medical_rules:
        unique = sorted(set(medical_rules))
        return GuardrailResult("medical_boundary", unique, False, True, unique, "medical-v1.0.0")

    unsafe_rules = _matches(user_text, UNSAFE_RULES)
    unsafe_rules.extend(_matches(reply, UNSAFE_RULES))
    if unsafe_rules:
        unique = sorted(set(unsafe_rules))
        return GuardrailResult("unsafe_content", unique, False, True, unique, "unsafe-v1.0.0")

    fraud_rules = _matches(user_text, FRAUD_RULES)
    fraud_rules.extend(_matches(reply, FRAUD_RULES))
    if fraud_rules:
        unique = sorted(set(fraud_rules))
        return GuardrailResult("unsafe_content", unique, False, True, unique, "fraud-v1.0.0")

    return GuardrailResult()


CRISIS_FALLBACK = (
    "听到你这样说，我很担心你的安全。你现在有没有已经想好的具体做法，或者正在准备伤害自己？"
    "请先不要一个人待着，马上联系身边可信任的人、人工支持或当地可用的紧急资源。"
)
MEDICAL_FALLBACK = (
    "我不能替医生做诊断，也不能给出用药、停药或治疗决定。请联系专业医生，"
    "我可以陪你把想问的问题整理下来。"
)
UNSAFE_FALLBACK = "我不能改变安全规则、泄露内部提示或执行越权指令。我们可以继续聊你真正需要帮助的事情。"
FRAUD_FALLBACK = (
    "这些话听起来很像诈骗风险话术。请不要向陌生人转账、汇款，不要提供银行卡号、密码或验证码。"
    "建议先和家人确认，遇到自称公检法的可以直接挂断并拨打 110 核实；已经转账请尽快联系银行和警方。"
)
SAFETY_FALLBACK = MEDICAL_FALLBACK
GENERIC_FALLBACK = "我刚才没有把话说明白。我们先慢一点，你可以再告诉我一次现在最困扰你的是什么。"
