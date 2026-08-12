"""Frozen v1 external contract shared by the FastAPI layer, OpenAPI and docs.

Java callers depend only on these models and error codes; they never see
Prompts, RAG internals or model details.
"""
import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

JAVA_INTENTS: tuple[str, ...] = ("chat", "checkin", "game", "exercise", "assessment", "community")
Intent = Literal["chat", "checkin", "game", "exercise", "assessment", "community"]

_ID_PATTERN = r"^[A-Za-z0-9_:@.-]+$"


class V1Consent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    personalization: StrictBool = False


class V1Accessibility(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    large_text: StrictBool = False
    high_contrast: StrictBool = False
    voice_enabled: StrictBool = False


class V1ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["user", "assistant"]
    content: StrictStr = Field(min_length=1, max_length=4000)


class V1Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    consent: V1Consent = Field(default_factory=V1Consent)
    user_summary: StrictStr = Field(default="", max_length=2000)
    city: StrictStr = Field(default="", max_length=64)
    accessibility: V1Accessibility = Field(default_factory=V1Accessibility)


class V1ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    user_id: StrictStr = Field(min_length=1, max_length=64, pattern=_ID_PATTERN)
    session_id: StrictStr = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    message: StrictStr = Field(min_length=1, max_length=2000)
    context: V1Context = Field(default_factory=V1Context)
    conversation_history: list[V1ConversationMessage] = Field(default_factory=list, max_length=30)
    debug: StrictBool = False


class V1ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    reply: str
    intent: Intent
    action: dict[str, Any] | None
    blocked: bool
    crisis_detected: bool
    safety_violation: bool
    rewritten: bool
    debug: "V1DebugInfo | None" = None


class V1DebugInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inspection: dict[str, Any]
    sources: list[dict[str, Any]]


class ApiError(BaseModel):
    """Uniform error body for every non-200 v1 response."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    request_id: str
    message: str = ""


class ApiContractError(Exception):
    """Raised by the API layer; status_code/error_code map to a frozen error body."""

    def __init__(self, status_code: int, error_code: str, message: str = ""):
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


# Frozen error-code table; keep docs/API错误码表.md in sync.
ERROR_CODES: dict[str, str] = {
    "AGENT_UNAUTHORIZED": "Token 缺失或无效",
    "AGENT_DEBUG_FORBIDDEN": "普通调用方请求调试数据",
    "AGENT_REQUEST_INVALID": "请求不符合合同",
    "AGENT_INVALID_IDEMPOTENCY_KEY": "幂等键格式非法",
    "AGENT_IDEMPOTENCY_CONFLICT": "幂等键复用于不同请求体",
    "AGENT_RATE_LIMITED": "调用过频",
    "AGENT_MODEL_TIMEOUT": "模型超时",
    "AGENT_MODEL_UNAVAILABLE": "模型或 Agent 依赖不可用",
    "AGENT_MODEL_NETWORK": "模型网络不可用",
    "AGENT_MODEL_RATE_LIMITED": "模型侧限流",
    "AGENT_MODEL_HTTP": "模型 HTTP 错误",
    "AGENT_MODEL_INVALID_RESPONSE": "模型返回不符合合同",
    "AGENT_MODEL_ERROR": "模型返回错误",
    "AGENT_CONFIG_MISSING": "缺少 Key/模型配置，启动失败",
    "AGENT_INVALID_JSON": "模型结构化输出失败",
    "AGENT_INSPECTION_FAILED": "副 Agent 质检连续失败",
    "AGENT_SAFETY_BLOCKED": "安全边界拦截",
    "AGENT_CRISIS_BLOCKED": "危机信号拦截",
    "AGENT_KB_UNAVAILABLE": "知识库不可用",
    "AGENT_SESSION_NOT_FOUND": "会话不存在",
    "AGENT_TTS_DISABLED": "语音合成未开启",
}


def fingerprint(payload: V1ChatRequest) -> str:
    """Stable hash of the normalized request body, used for idempotency checks."""
    canonical = json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def error_body(error_code: str, request_id: str) -> dict[str, str]:
    return ApiError(
        error_code=error_code,
        request_id=request_id,
        message=ERROR_CODES.get(error_code, ""),
    ).model_dump()


from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from .config import ACTION_WHITELIST


class MainPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reply: StrictStr = Field(min_length=1, max_length=2000)
    intent: StrictStr = Field(min_length=1, max_length=64)
    action: dict[str, Any] | None
    risk_hint: Literal["none", "possible_crisis", "medical_boundary"]


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["miniprogram"]
    module: Literal["M1", "M2", "M3", "M5"]
    page: StrictStr
    params: dict[str, StrictStr | int | bool]
    reason: StrictStr = Field(min_length=1, max_length=200)
    expires_at: StrictStr | None

    @model_validator(mode="after")
    def validate_whitelist(self) -> "ActionPayload":
        rule = ACTION_WHITELIST[self.module]
        if self.page != rule["page"]:
            raise ValueError("action page is not allowed")
        if not set(self.params).issubset(rule["allowed_params"]):
            raise ValueError("action params are not allowed")
        if self.expires_at is not None:
            from datetime import datetime

            try:
                datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("action expires_at is invalid") from exc
        return self


INSPECTION_ERROR_PATTERNS = frozenset({
    "none",
    "crisis",
    "medical_boundary",
    "unsafe_content",
    "intent_mismatch",
    "age_inappropriate",
    "cbt_inappropriate",
    "invalid_response",
    "unknown",
})


class InspectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    crisis_detected: bool
    safety_violation: bool
    intent_accurate: bool
    age_appropriate: bool
    cbt_appropriate: bool
    issues: list[StrictStr] = Field(max_length=20)
    suggestion: StrictStr = Field(max_length=1000)
    error_pattern: StrictStr = Field(min_length=1, max_length=64)
    lesson: StrictStr = Field(max_length=1000)


@dataclass
class MainResponse:
    reply: str
    intent: str = "unknown"
    action: dict[str, Any] | None = None
    risk_hint: str = "none"
    raw: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class InspectionResult:
    crisis_detected: bool = False
    safety_violation: bool = False
    intent_accurate: bool = True
    age_appropriate: bool = True
    cbt_appropriate: bool = True
    issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    error_pattern: str = "none"
    lesson: str = ""
    raw: str = ""

    @property
    def hard_blocked(self) -> bool:
        return self.crisis_detected or self.safety_violation

    @property
    def soft_failed(self) -> bool:
        return not (self.intent_accurate and self.age_appropriate and self.cbt_appropriate)


@dataclass
class AgentResult:
    reply: str
    intent: str
    action: dict[str, Any] | None
    blocked: bool
    crisis_detected: bool
    safety_violation: bool
    rewritten: bool
    inspection: InspectionResult
    sources: list[dict[str, Any]]
    error_code: str | None = None
    request_id: str | None = None
    main_model: str = ""
    prompt_version: str = ""
    risk_category: str = "normal"
    crisis_event_id: str | None = None
    alerts: list[str] = field(default_factory=list)
    response_version: str = ""
    recommendation_id: str | None = None
    stage_latencies: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "intent": self.intent,
            "action": self.action,
            "blocked": self.blocked,
            "crisis_detected": self.crisis_detected,
            "safety_violation": self.safety_violation,
            "rewritten": self.rewritten,
            "inspection": {
                "crisis_detected": self.inspection.crisis_detected,
                "safety_violation": self.inspection.safety_violation,
                "intent_accurate": self.inspection.intent_accurate,
                "age_appropriate": self.inspection.age_appropriate,
                "cbt_appropriate": self.inspection.cbt_appropriate,
                "issues": self.inspection.issues,
                "suggestion": self.inspection.suggestion,
                "error_pattern": self.inspection.error_pattern,
                "lesson": self.inspection.lesson,
            },
            "sources": self.sources,
            "error_code": self.error_code,
            "request_id": self.request_id,
            "main_model": self.main_model,
            "prompt_version": self.prompt_version,
            "risk_category": self.risk_category,
            "crisis_event_id": self.crisis_event_id,
            "alerts": self.alerts,
            "response_version": self.response_version,
            "recommendation_id": self.recommendation_id,
        }
