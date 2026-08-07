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


class V1Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    consent: V1Consent = Field(default_factory=V1Consent)
    user_summary: StrictStr = Field(default="", max_length=2000)


class V1ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    user_id: StrictStr = Field(min_length=1, max_length=64, pattern=_ID_PATTERN)
    session_id: StrictStr = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    message: StrictStr = Field(min_length=1, max_length=2000)
    context: V1Context = Field(default_factory=V1Context)
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
