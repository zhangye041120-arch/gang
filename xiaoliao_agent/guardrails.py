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
    RiskRule("C006", r"拖累(?:孩子|儿女|家人|儿子|女儿|别人|大家)|(?:家里|孩子|儿女)的?(?:拖累|累赘)|是(?:个|大家的)?(?:累赘|负担)"),
    RiskRule("C007", r"(?:一觉|睡)(?:过去|着)?(?:就)?(?:别|不要|不)(?:醒|起来)|(?:睡着了|睡下去)(?:就)?(?:别|不要|不)(?:醒|起来)"),
    RiskRule("C008", r"活着(?:就是|只会|只是|也是)?(?:拖累|负担|给别人添麻烦)|(?:我)?(?:就是|是个)(?:大家的|家里的)?(?:累赘|负担)"),
    RiskRule("C009", r"(?:没|没有|要是没|要没)(?:有)?我(?:你们|大家|家人)?(?:会|就|也)?(?:更|过得)?(?:好|轻松)|(?:最好|不如|巴不得)(?:一觉)?(?:睡过去|睡着|闭眼)(?:别醒|不再醒|醒不来)"),
    RiskRule("C010", r"活够了|活到头了|早点(?:走|解脱)|一了百了"),
    RiskRule("C011", r"(?:没|没有)(?:了)?我?(?:，|,)?(?:他们|大家|你们|家人)?(?:会|就|也)?(?:过)?(?:得)?(?:更好|更轻松|更省心)|(?:我|我要是|要是)(?:没了|不在|走了)(?:，|,)?(?:他们|大家|你们|家人)(?:会|就|也)?(?:过)?(?:得)?(?:更好|更轻松|更省心)"),
    RiskRule("C012", r"(?:是|成了|成为|变成|觉得自己是|觉得我是个)(?:大家的|家里的|个)?累赘|很累赘"),
)

# 医疗边界只拦明确的个人诊断/用药/停药/治疗决策；陈述、提醒、科普和转述医嘱不拦截。
MEDICAL_INPUT_RULES = (
    RiskRule(
        "M001",
        r"(?:请|帮我|帮我看看|能否|可以|能不能|麻烦帮我).{0,10}(?:诊断|判断).{0,12}(?:我|我的|是不是|得了)|"
        r"我(?:是不是|是不是得了|是不是有|是不是患|是否|是否得了|是否有|是否患).{0,8}(?:抑郁|焦虑|失眠|躁郁|神经衰弱)",
    ),
    RiskRule(
        "M002",
        r"(?:我|咱|我们)(?:该|应该|要|得|能|可以|能不能|可不可以).{0,10}(?:吃什么药|吃啥药|用什么药|用啥药|吃多少|用多少|多大剂量|剂量多少|开药|开处方|配药|换药)|"
        r"(?<!医生)(?:请|帮我|给我|麻烦帮我).{0,8}(?:开|配|换).{0,6}(?:药|处方|药方)|"
        r"(?:这个|这种|这些|那个|那种).{0,4}药.{0,14}(?:吃多少|用多少|多大剂量|剂量多少|该吃|应该吃|能吃|可以吃|该用|应该用|能用|可以用|能停|可以停|该停|应该停|能换|可以换|该换|应该换)",
    ),
    RiskRule(
        "M003",
        r"(?:我|咱|我们)(?:现在)?(?:能不能|该不该|应不应该|可不可以|要不要|是否|该|应该|可以|能|需要|想|要).{0,8}(?:停药|加药|减药|加大剂量|减少剂量|加量|减量)(?!提醒)|"
        r"(?:我觉得我|我认为我|我感觉我)(?:该|应该|需要|想|要|可以|能不能|可不可以).{0,8}(?:停药|加药|减药|加大剂量|减少剂量|加量|减量)|"
        r"(?:这个|这种|这些|那个|那种).{0,4}药.{0,12}(?:能不能|可不可以|该不该|要不要|需要|应该|可以|能|要).{0,8}(?:停|加|减|停药|加药|减药|加量|减量)|"
        r"(?:把|能不能把|可不可以把).{0,6}(?:这个|这种)?药.{0,4}(?:停|停掉|停了|加|减)",
    ),
    RiskRule(
        "M004",
        r"(?:保证|一定|肯定).{0,10}(?:治好|治愈)|"
        r"(?:我|咱|我们|这个病|我的病).{0,12}(?:该|应该|可以|适合|要不要|能不能|有没有|怎么).{0,8}(?:治疗|治疗方案|治好|怎么治)",
    ),
)

MEDICAL_OUTPUT_RULES = (
    RiskRule(
        "M101",
        r"(?:你|您)(?:患有|得了|确诊|被诊断为)|"
        r"(?:我判断|我认为|我诊断|我确诊)(?:你|您)(?:患有|得了|是|为).{0,8}(?:抑郁|焦虑|失眠|高血压|糖尿病)",
    ),
    RiskRule(
        "M102",
        r"(?:建议|可以|应该|需要|最好|别|不要|不用|继续|马上|先|不建议|不能)(?:你|您|咱|我们|我)?(?:停药|加药|减药|加大剂量|减少剂量|加量|减量)|"
        r"(?:你|您|咱|我们|我)(?:建议|可以|应该|需要|最好|别|不要|不用|继续|马上|先|不建议|不能)(?:停药|加药|减药|加大剂量|减少剂量|加量|减量)|"
        r"(?:建议|可以|应该|需要|最好|别|不要|不用|继续|马上|先|不建议|不能)(?:你|您)?(?:把).{0,6}药.{0,4}(?:停|停掉|停了|加|减)|"
        r"(?:你|您)(?:建议|可以|应该|需要|最好|别|不要|不用|继续|马上|先|不建议|不能)(?:把).{0,6}药.{0,4}(?:停|停掉|停了|加|减)|"
        r"(?:我给你|我帮你|我可以给你|我可以帮你|我来给你|我来帮你|建议|可以|应该|需要|最好|别|不要|不用|继续|马上|先|不建议|不能)(?:你|您)?(?:开处方|开药|开药方|开个处方)",
    ),
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
    append_disclaimer: bool = False


def _matches(text: str, rules: tuple[RiskRule, ...]) -> list[str]:
    return [rule.rule_id for rule in rules if re.search(rule.pattern, text, re.I)]


def medical_input_matches(text: str) -> list[str]:
    """Return rule IDs for explicit personal diagnosis/medication/treatment requests."""
    return _matches(text, MEDICAL_INPUT_RULES)


def _crisis_matches(text: str, *, honor_context: bool) -> list[str]:
    if honor_context and (NEGATED_CRISIS.search(text) or REPORTED_CRISIS.search(text)):
        return []
    return _matches(text, CRISIS_RULES)


def precheck(user_text: str, reply: str = "") -> GuardrailResult:
    # Crisis and fraud are always hard-blocked on both input and output.
    crisis_rules = _crisis_matches(user_text, honor_context=True)
    crisis_rules.extend(_crisis_matches(reply, honor_context=False))
    if crisis_rules:
        unique = sorted(set(crisis_rules))
        return GuardrailResult("crisis", unique, True, False, unique, "crisis-v1.0.0")

    # Medical boundary: only check the MODEL's reply, never the user's input.
    # The model is allowed to answer; if its reply contains diagnosis, drug
    # dosage, or treatment decisions, we append a disclaimer rather than
    # blocking the entire response.
    if reply:
        medical_rules = _matches(reply, MEDICAL_OUTPUT_RULES)
        if medical_rules:
            unique = sorted(set(medical_rules))
            return GuardrailResult(
                "medical_boundary", unique, False, True, unique,
                "medical-v1.0.0", append_disclaimer=True,
            )

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


MEDICAL_DISCLAIMER = (
    "\n我不能替医生做诊断，也不能给出用药、停药或治疗决定。"
    "请联系专业医生，我可以陪你把想问的问题整理下来。"
)

CRISIS_FALLBACK = (
    "听到你这样说，我很担心你的安全。你现在有没有已经想好的具体做法，或者正在准备伤害自己？"
    "请先不要一个人待着，马上联系身边可信任的人，或拨打全国心理援助热线 12356；"
    "情况紧急请直接拨打 120/110。"
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
GENERIC_FALLBACK = "我刚才没能把这句话接稳。请再说一次，我会接着刚才的话回答。"
