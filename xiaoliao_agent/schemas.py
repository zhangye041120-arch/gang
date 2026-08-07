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
