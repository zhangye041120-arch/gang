import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
import hmac
import json
import logging
import uuid
from time import monotonic
from re import fullmatch
from threading import Lock
from typing import Any, Callable, Literal

from fastapi import FastAPI, Query, Request, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from xiaoliao_agent import Settings, XiaoliaoAgent
from xiaoliao_agent.agent import to_java_intent
from xiaoliao_agent.content_refs import HmacReferenceService
from xiaoliao_agent.gateway_auth import (
    GatewayAuthenticationError,
    GatewayAuthenticator,
)
from xiaoliao_agent.privacy import (
    PrivacyDeletionService,
    SubjectDeletedError,
    SubjectPrivacyGuard,
)
from xiaoliao_agent.api_contract import (
    ActionEventRequest,
    ActionEventResponse,
    ApiError,
    ApiContractError,
    PrivacyDeletionResponse,
    V1ChatRequest,
    V1ChatResponse,
    V1DebugInfo,
    error_body,
    fingerprint,
)
from xiaoliao_agent.reminders import (
    DailyCheckinService,
    checkin_policy_from_settings,
)
from xiaoliao_agent.actions import (
    ActionContractError,
    ActionEvent,
    ActionEventConflict,
    ActionMemoryUnavailable,
)
from xiaoliao_agent.migrations import verify_schema
from xiaoliao_agent.providers import OpenAICompatibleClient
from xiaoliao_agent.runtime import RuntimeReadiness
from xiaoliao_agent.runtime_state import RedisRuntimeState, RuntimeStateUnavailable
from xiaoliao_agent.runtime_state import RuntimeSubjectDeleted
from xiaoliao_agent.text_utils import chunk_by_graphemes
from xiaoliao_agent.user_data import (
    MemoryUserRepository,
    PostgresUserRepository,
    UserDataService,
)


Intent = Literal["chat", "checkin", "game", "exercise", "assessment", "community"]

logger = logging.getLogger("xiaoliao.api")

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(
        alias="userId",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_:@.-]+$",
    )
    message: str = Field(min_length=1, max_length=2000)
    conversation_history: list[ConversationMessage] = Field(
        default_factory=list,
        alias="conversationHistory",
        max_length=30,
    )


class ChatResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    reply: str
    intent: Intent
    inspection: dict[str, Any] = Field(default_factory=dict)
    retrieved_contexts: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="retrievedContexts",
    )


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_:@.-]+$",
    )
    personalization: bool = False
    sensitive: bool = False


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class DeleteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_:@.-]+$",
    )


class MemoryCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_:@.-]+$",
    )
    content: str = Field(min_length=1, max_length=1000)


class SlidingWindowRateLimiter:
    def __init__(self, per_minute: int, per_user: int):
        self.per_minute = max(1, per_minute)
        self.per_user = max(1, per_user)
        self._events: dict[str, list[float]] = {}
        self._lock = Lock()

    def allow(self, principal: str, user_id: str) -> bool:
        now = monotonic()
        with self._lock:
            allowed = True
            for key, limit in ((f"principal:{principal}", self.per_minute), (f"user:{user_id}", self.per_user)):
                events = [stamp for stamp in self._events.get(key, []) if now - stamp < 60]
                if len(events) >= limit:
                    allowed = False
                else:
                    events.append(now)
                self._events[key] = events
            return allowed


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


class IdempotencyCoordinator:
    """Single-process coordination so retries never re-enter the Agent."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}

    async def claim(self, principal: str, key: str, request_fingerprint: str) -> tuple[str, tuple[int, dict[str, Any]] | None]:
        async with self._lock:
            entry = self._entries.get((principal, key))
            if entry is None:
                self._entries[(principal, key)] = {
                    "fingerprint": request_fingerprint,
                    "status": "executing",
                    "result": None,
                    "condition": asyncio.Condition(self._lock),
                }
                return "execute", None
            if entry["fingerprint"] != request_fingerprint:
                return "conflict", None
            if entry["status"] == "done":
                return "cached", entry["result"]
            condition: asyncio.Condition = entry["condition"]
            while entry["status"] == "executing":
                await condition.wait()
            return "cached", entry["result"]

    async def finish(self, principal: str, key: str, status_code: int, body: dict[str, Any]) -> None:
        async with self._lock:
            entry = self._entries.get((principal, key))
            if entry is None:
                return
            entry["status"] = "done"
            entry["result"] = (status_code, body)
            entry["condition"].notify_all()


def normalize_intent(raw_intent: str, action: dict[str, Any] | None) -> Intent:
    """Map internal Agent intents to Java's six-value contract."""
    return to_java_intent(raw_intent, action)


def log_conversation_pair_safe(
    user_data: Any,
    user_id: str,
    *,
    user_text: str,
    reply: str,
    intent: str,
    request_id: str,
) -> None:
    try:
        user_data.log_conversation_pair(
            user_id,
            user_text=user_text,
            reply=reply,
            intent=intent,
            request_id=request_id,
        )
    except Exception:
        pass


def inspection_payload(result: Any) -> dict[str, Any]:
    data = result.to_dict().get("inspection", {})
    return {
        "crisis_detected": bool(data.get("crisis_detected", False)),
        "safety_violation": bool(data.get("safety_violation", False)),
        "intent_accurate": bool(data.get("intent_accurate", True)),
        "age_appropriate": bool(data.get("age_appropriate", True)),
        "cbt_appropriate": bool(data.get("cbt_appropriate", True)),
        "issues": data.get("issues", []),
        "error_pattern": data.get("error_pattern", "none"),
        "lesson": data.get("lesson", ""),
    }


def create_app(
    agent_factory: Callable[[], XiaoliaoAgent] | None = None,
    *,
    settings: Settings | None = None,
    api_token: str | None = None,
    debug_token: str | None = None,
    test_mode: bool = False,
    user_data_service: Any | None = None,
    runtime_state: Any | None = None,
    readiness_service: Any | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    if api_token is not None:
        config = replace(config, api_token=api_token)
    if debug_token is not None:
        config = replace(config, api_debug_token=debug_token)
    effective_test_mode = test_mode or config.api_test_mode
    factory = agent_factory or (lambda: XiaoliaoAgent(config))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.is_production:
            if test_mode:
                raise RuntimeError("production cannot start in test mode")
            config.validate_production()
        if not effective_test_mode and not config.api_token:
            raise RuntimeError("API_TOKEN 未配置，生产 API 拒绝启动")
        owned_runtime_state = False
        selected_runtime_state = runtime_state
        if config.is_production and selected_runtime_state is None:
            selected_runtime_state = RedisRuntimeState.from_url(
                config.redis_url,
                prefix=config.redis_key_prefix,
                idempotency_ttl_seconds=config.idempotency_ttl_seconds,
                execution_ttl_seconds=config.idempotency_execution_ttl_seconds,
                nonce_ttl_seconds=config.nonce_ttl_seconds,
            )
            owned_runtime_state = True
        if config.is_production:
            await selected_runtime_state.ping()
            await run_in_threadpool(verify_schema, config.knowledge_database_url)
        app.state.runtime_state = selected_runtime_state
        app.state.agent = factory()
        reference_service = getattr(app.state.agent, "reference_service", None)
        if reference_service is None:
            reference_service = HmacReferenceService(
                config.privacy_hmac_secret or config.quality_hash_salt,
                key_version=config.privacy_hmac_key_version,
                previous_keys=config.privacy_hmac_previous_keys,
            )
        app.state.reference_service = reference_service
        app.state.privacy_guard = getattr(
            app.state.agent, "privacy_guard", None
        ) or SubjectPrivacyGuard(reference_service)
        app.state.gateway_authenticator = None
        if config.is_production:
            app.state.gateway_authenticator = GatewayAuthenticator(
                config.gateway_hmac_secret,
                selected_runtime_state,
                clock_skew_seconds=config.gateway_clock_skew_seconds,
                reference_service=HmacReferenceService(
                    config.privacy_hmac_secret,
                    key_version=config.privacy_hmac_key_version,
                    previous_keys=config.privacy_hmac_previous_keys,
                ),
            )
        app.state.rate_limiter = SlidingWindowRateLimiter(
            config.api_rate_limit_per_minute,
            config.api_rate_limit_per_user,
        )
        if user_data_service is not None:
            app.state.user_data = user_data_service
        elif config.knowledge_database_url and not effective_test_mode:
            app.state.user_data = UserDataService(
                PostgresUserRepository(config.knowledge_database_url),
                memory_service=getattr(app.state.agent, "memory_service", None),
                reference_service=getattr(app.state.agent, "reference_service", None),
            )
        else:
            app.state.user_data = UserDataService(
                MemoryUserRepository(),
                memory_service=getattr(app.state.agent, "memory_service", None),
                reference_service=getattr(app.state.agent, "reference_service", None),
            )
        for candidate in (
            getattr(app.state.user_data, "repository", None),
            getattr(getattr(app.state.agent, "memory_service", None), "repository", None),
            getattr(getattr(app.state.agent, "action_service", None), "repository", None),
            getattr(getattr(app.state.agent, "reminder_service", None), "repository", None),
            getattr(app.state.agent, "crisis_repository", None),
            getattr(getattr(app.state.agent, "quality_service", None), "repository", None),
        ):
            if candidate is not None and hasattr(candidate, "privacy_guard"):
                candidate.privacy_guard = app.state.privacy_guard
        app.state.privacy_deletion = PrivacyDeletionService(
            reference_service=app.state.reference_service,
            privacy_guard=app.state.privacy_guard,
            user_data_service=app.state.user_data,
            memory_service=getattr(app.state.agent, "memory_service", None),
            action_service=getattr(app.state.agent, "action_service", None),
            reminder_service=getattr(app.state.agent, "reminder_service", None),
            crisis_repository=getattr(app.state.agent, "crisis_repository", None),
            quality_service=getattr(app.state.agent, "quality_service", None),
            lesson_repository=getattr(app.state.agent, "lesson_repository", None),
            runtime_state=selected_runtime_state,
            database_url=(
                config.knowledge_database_url
                if config.is_production
                and isinstance(app.state.user_data.repository, PostgresUserRepository)
                else ""
            ),
            legacy_quality_salt=config.quality_hash_salt,
        )
        app.state.idempotency = IdempotencyCoordinator()
        app.state.readiness = readiness_service or RuntimeReadiness(
            config,
            lambda: app.state.agent,
            selected_runtime_state,
        )
        app.state.checkin_reminder = None
        app.state.checkin_reminder_task = None
        if config.wecom_checkin_reminder_enabled:
            checkin_policy = checkin_policy_from_settings(config)
            if checkin_policy.user_schedules and not (
                config.wecom_corp_id
                and config.wecom_agent_id
                and config.wecom_agent_secret
            ):
                raise RuntimeError(
                    "用户级签到提醒需要 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_AGENT_SECRET"
                )
            app.state.checkin_reminder = DailyCheckinService(
                checkin_policy
            )

            async def checkin_loop() -> None:
                while True:
                    try:
                        events = await run_in_threadpool(
                            app.state.checkin_reminder.run_due
                        )
                        for event in events:
                            if event.get("status") in {"sent", "failed"}:
                                logger.warning("checkin reminder %s", event)
                    except Exception:
                        logger.exception("checkin reminder loop error")
                    await asyncio.sleep(60)

            app.state.checkin_reminder_task = asyncio.create_task(checkin_loop())
        yield
        if app.state.checkin_reminder_task is not None:
            app.state.checkin_reminder_task.cancel()
            try:
                await app.state.checkin_reminder_task
            except asyncio.CancelledError:
                pass
        if selected_runtime_state is not None and (owned_runtime_state or runtime_state is not None):
            await selected_runtime_state.aclose()
        OpenAICompatibleClient.close_all()

    app = FastAPI(
        title="小辽 M7 Agent API",
        description="Java -> Python Agent: DeepSeek 主 Agent + Qwen 质检 + CBT RAG",
        version="1.1.0",
        default_response_class=UTF8JSONResponse,
        lifespan=lifespan,
    )

    error_responses = {
        401: {"model": ApiError},
        403: {"model": ApiError},
        410: {"model": ApiError},
        404: {"model": ApiError},
        409: {"model": ApiError},
        422: {"model": ApiError},
        429: {"model": ApiError},
        502: {"model": ApiError},
        503: {"model": ApiError},
        504: {"model": ApiError},
    }

    def authorize(request: Request) -> tuple[str, bool]:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token_matches_api = bool(config.api_token) and hmac.compare_digest(token, config.api_token)
        token_matches_debug = bool(config.api_debug_token) and hmac.compare_digest(token, config.api_debug_token)
        if scheme.lower() != "bearer" or not token or not (token_matches_api or token_matches_debug):
            raise ApiContractError(401, "AGENT_UNAUTHORIZED")
        principal = sha256(token.encode("utf-8")).hexdigest()[:16]
        is_debug = token_matches_debug
        return principal, is_debug

    async def authorize_user(
        request: Request,
        expected_user_id: str,
        *,
        allow_deleted: bool = False,
    ) -> tuple[str, bool]:
        if not config.is_production:
            principal, is_debug = authorize(request)
        else:
            _service_principal, is_debug = authorize(request)
            try:
                gateway_principal = await app.state.gateway_authenticator.verify(
                    request,
                    await request.body(),
                    expected_user_id,
                )
            except GatewayAuthenticationError as exc:
                raise ApiContractError(401, "AGENT_UNAUTHORIZED") from exc
            except RuntimeStateUnavailable as exc:
                raise ApiContractError(
                    503, "AGENT_RUNTIME_STATE_UNAVAILABLE"
                ) from exc
            principal = gateway_principal.subject_hmac
        if not allow_deleted:
            try:
                deleted = await run_in_threadpool(
                    app.state.privacy_deletion.is_deleted,
                    expected_user_id,
                )
            except Exception as exc:
                raise ApiContractError(
                    503, "AGENT_RUNTIME_STATE_UNAVAILABLE"
                ) from exc
            if deleted:
                raise ApiContractError(410, "AGENT_SUBJECT_DELETED")
        return principal, is_debug

    def request_id(request: Request) -> str:
        candidate = request.headers.get("x-request-id", "")
        if fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", candidate or ""):
            return candidate
        return uuid.uuid4().hex

    def json_response(status_code: int, body: dict[str, Any], rid: str) -> UTF8JSONResponse:
        response = UTF8JSONResponse(status_code=status_code, content=body)
        response.headers["X-Request-ID"] = rid
        return response

    async def allow_request(principal: str, user_id: str) -> bool:
        state = app.state.runtime_state
        if state is None:
            return app.state.rate_limiter.allow(principal, user_id)
        try:
            return await state.allow_rate(
                principal,
                user_id,
                per_minute=config.api_rate_limit_per_minute,
                per_user=config.api_rate_limit_per_user,
            )
        except RuntimeStateUnavailable as exc:
            raise ApiContractError(503, "AGENT_RUNTIME_STATE_UNAVAILABLE") from exc
        except RuntimeSubjectDeleted as exc:
            raise ApiContractError(410, "AGENT_SUBJECT_DELETED") from exc

    async def claim_idempotency(
        principal: str,
        key: str,
        request_fingerprint: str,
    ) -> tuple[str, tuple[int, dict[str, Any]] | None, str]:
        state = app.state.runtime_state
        if state is None:
            decision, cached = await app.state.idempotency.claim(
                principal,
                key,
                request_fingerprint,
            )
            return decision, cached, ""
        try:
            claim = await state.claim_idempotency(
                principal,
                key,
                request_fingerprint,
            )
        except RuntimeStateUnavailable as exc:
            raise ApiContractError(503, "AGENT_RUNTIME_STATE_UNAVAILABLE") from exc
        except RuntimeSubjectDeleted as exc:
            raise ApiContractError(410, "AGENT_SUBJECT_DELETED") from exc
        cached = None
        if claim.decision == "cached":
            cached = (claim.status_code or 200, claim.response or {})
        return claim.decision, cached, claim.execution_token

    async def finish_idempotency(
        principal: str,
        key: str,
        execution_token: str,
        status_code: int,
        body: dict[str, Any],
    ) -> None:
        state = app.state.runtime_state
        if state is None:
            await app.state.idempotency.finish(principal, key, status_code, body)
            return
        try:
            completed = await state.finish_idempotency(
                principal,
                key,
                execution_token,
                status_code,
                body,
            )
        except RuntimeStateUnavailable as exc:
            raise ApiContractError(503, "AGENT_RUNTIME_STATE_UNAVAILABLE") from exc
        if not completed:
            raise ApiContractError(503, "AGENT_RUNTIME_STATE_UNAVAILABLE")

    def _stream_response(body: dict[str, Any], rid: str) -> StreamingResponse:
        async def events():
            reply = str(body.get("reply", ""))
            for chunk in chunk_by_graphemes(reply, 4) or [""]:
                yield (
                    "event: message\n"
                    f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
                )
            done = {
                key: body[key]
                for key in (
                    "session_id", "intent", "action", "blocked",
                    "crisis_detected", "safety_violation", "rewritten",
                )
                if key in body
            }
            if "debug" in body:
                done["debug"] = body["debug"]
            yield f"event: done\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream; charset=utf-8",
            headers={"X-Request-ID": rid, "Cache-Control": "no-cache"},
        )

    @app.exception_handler(ApiContractError)
    async def api_contract_error_handler(request: Request, exc: ApiContractError) -> UTF8JSONResponse:
        rid = request_id(request)
        return json_response(exc.status_code, error_body(exc.error_code, rid), rid)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> UTF8JSONResponse:
        rid = request_id(request)
        return json_response(422, error_body("AGENT_REQUEST_INVALID", rid), rid)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready(request: Request):
        result = await app.state.readiness.check()
        return json_response(
            200 if result.ready else 503,
            {"status": "ready" if result.ready else "not_ready"},
            request_id(request),
        )

    @app.get("/health", deprecated=True)
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.post(
        "/chat",
        response_model=ChatResponse,
        response_model_by_alias=True,
        deprecated=True,
        responses=error_responses,
    )
    async def chat(
        payload: ChatRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, payload.user_id)
        rid = request_id(request)
        agent: XiaoliaoAgent = app.state.agent
        history = [item.model_dump() for item in payload.conversation_history]

        try:
            result = await run_in_threadpool(
                agent.chat,
                payload.message,
                history,
                "",
                payload.user_id,
                "",
                request_id=rid,
            )
        except Exception:
            body = ChatResponse(
                reply="我现在有点忙，没能马上听清楚。请过一会儿再和我说说，好吗？",
                intent="chat",
                inspection={"error": "AGENT_MODEL_UNAVAILABLE"},
                retrievedContexts=[],
            ).model_dump(by_alias=True)
        else:
            body = ChatResponse(
                reply=result.reply,
                intent=normalize_intent(result.intent, result.action),
                inspection=inspection_payload(result),
                retrieved_contexts=result.sources,
            ).model_dump(by_alias=True)
        log_conversation_pair_safe(
            app.state.user_data,
            payload.user_id,
            user_text=payload.message,
            reply=body["reply"],
            intent=body["intent"],
            request_id=rid,
        )
        return json_response(200, body, rid)

    @app.post("/v1/chat", response_model=V1ChatResponse, responses=error_responses)
    async def v1_chat(
        payload: V1ChatRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        principal, is_debug = await authorize_user(request, payload.user_id)
        rid = request_id(request)
        if payload.debug and not is_debug:
            raise ApiContractError(403, "AGENT_DEBUG_FORBIDDEN")
        if not await allow_request(principal, payload.user_id):
            return json_response(429, error_body("AGENT_RATE_LIMITED", rid), rid)
        effective_personalization, _ = app.state.user_data.effective_consent(
            payload.user_id,
            payload.context.consent.personalization,
            False,
        )
        effective_summary = (
            payload.context.user_summary if effective_personalization else ""
        )
        effective_context = payload.context.model_dump()
        effective_context["consent"]["personalization"] = effective_personalization
        effective_context["user_summary"] = effective_summary
        history = [item.model_dump() for item in payload.conversation_history]
        idem = request.headers.get("idempotency-key", "")
        executing = False
        execution_token = ""
        if idem:
            if len(idem) > 128 or not fullmatch(r"[A-Za-z0-9_.:@-]+", idem):
                raise ApiContractError(422, "AGENT_INVALID_IDEMPOTENCY_KEY")
            state, cached, execution_token = await claim_idempotency(
                principal, idem, fingerprint(payload)
            )
            if state == "conflict":
                raise ApiContractError(409, "AGENT_IDEMPOTENCY_CONFLICT")
            if state == "in_progress":
                raise ApiContractError(409, "AGENT_IDEMPOTENCY_IN_PROGRESS")
            if state == "cached":
                status, cached_body = cached
                return json_response(status, cached_body, rid)
            executing = True
        try:
            result = await run_in_threadpool(
                app.state.agent.chat,
                payload.message,
                history,
                effective_summary,
                payload.user_id,
                payload.session_id,
                effective_personalization,
                request_id=rid,
                context_prefs=effective_context,
            )
        except Exception:
            body = error_body("AGENT_MODEL_UNAVAILABLE", rid)
            if executing:
                await finish_idempotency(principal, idem, execution_token, 502, body)
            return json_response(502, body, rid)
        if result.error_code == "AGENT_MODEL_TIMEOUT":
            body = error_body(result.error_code, rid)
            if executing:
                await finish_idempotency(principal, idem, execution_token, 504, body)
            return json_response(504, body, rid)
        if result.error_code and result.error_code != "action_policy_violation":
            body = error_body(result.error_code, rid)
            if executing:
                await finish_idempotency(principal, idem, execution_token, 502, body)
            return json_response(502, body, rid)
        debug_payload = (
            V1DebugInfo(inspection=inspection_payload(result), sources=result.sources)
            if payload.debug and is_debug
            else None
        )
        response_body = V1ChatResponse(
            session_id=payload.session_id,
            reply=result.reply,
            intent=normalize_intent(result.intent, result.action),
            action=result.action,
            blocked=result.blocked,
            crisis_detected=result.crisis_detected,
            safety_violation=result.safety_violation,
            rewritten=result.rewritten,
            debug=debug_payload,
        ).model_dump(exclude={"debug"})
        if debug_payload is not None:
            response_body["debug"] = debug_payload.model_dump()
        log_conversation_pair_safe(
            app.state.user_data,
            payload.user_id,
            user_text=payload.message,
            reply=response_body["reply"],
            intent=response_body["intent"],
            request_id=rid,
        )
        if executing:
            await finish_idempotency(
                principal, idem, execution_token, 200, response_body
            )
        return json_response(200, response_body, rid)

    @app.post("/v1/chat/stream", responses=error_responses)
    async def v1_chat_stream(
        payload: V1ChatRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        principal, is_debug = await authorize_user(request, payload.user_id)
        rid = request_id(request)
        if payload.debug and not is_debug:
            raise ApiContractError(403, "AGENT_DEBUG_FORBIDDEN")
        if not await allow_request(principal, payload.user_id):
            return json_response(429, error_body("AGENT_RATE_LIMITED", rid), rid)

        effective_personalization, _ = app.state.user_data.effective_consent(
            payload.user_id,
            payload.context.consent.personalization,
            False,
        )
        effective_summary = (
            payload.context.user_summary if effective_personalization else ""
        )
        effective_context = payload.context.model_dump()
        effective_context["consent"]["personalization"] = effective_personalization
        effective_context["user_summary"] = effective_summary

        history = [item.model_dump() for item in payload.conversation_history]
        idem = request.headers.get("idempotency-key", "")
        executing = False
        execution_token = ""
        if idem:
            if len(idem) > 128 or not fullmatch(r"[A-Za-z0-9_.:@-]+", idem):
                raise ApiContractError(422, "AGENT_INVALID_IDEMPOTENCY_KEY")
            state, cached, execution_token = await claim_idempotency(
                principal, idem, fingerprint(payload)
            )
            if state == "conflict":
                raise ApiContractError(409, "AGENT_IDEMPOTENCY_CONFLICT")
            if state == "in_progress":
                raise ApiContractError(409, "AGENT_IDEMPOTENCY_IN_PROGRESS")
            if state == "cached":
                status, cached_body = cached
                if status != 200:
                    return json_response(status, cached_body, rid)
                return _stream_response(cached_body, rid)
            executing = True

        async def events():
            reply_parts: list[str] = []
            done_payload: dict[str, Any] = {}
            replaced = False
            try:
                gen = app.state.agent.chat_stream(
                    payload.message,
                    history,
                    effective_summary,
                    payload.user_id,
                    payload.session_id,
                    effective_personalization,
                    context_prefs=effective_context,
                )
                while True:
                    item = await run_in_threadpool(next, gen)
                    item_type = item.get("type")
                    if item_type == "status":
                        yield (
                            "event: status\n"
                            f"data: {json.dumps({'stage': item.get('stage')}, ensure_ascii=False)}\n\n"
                        )
                    elif item_type in {"token", "message"}:
                        if not replaced:
                            content = str(item.get("content", ""))
                            reply_parts.append(content)
                            yield (
                                "event: message\n"
                                f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
                            )
                    elif item_type == "corrected":
                        replaced = True
                        content = str(item.get("reply", ""))
                        reply_parts.append(content)
                        yield (
                            "event: message\n"
                            f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
                        )
                    elif item_type == "done":
                        done_payload = item
                        break
            except Exception:
                body = error_body("AGENT_MODEL_UNAVAILABLE", rid)
                if executing:
                    try:
                        await finish_idempotency(
                            principal, idem, execution_token, 502, body
                        )
                    except ApiContractError:
                        body = error_body("AGENT_RUNTIME_STATE_UNAVAILABLE", rid)
                yield "event: error\n" + f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
                return

            reply = "".join(reply_parts)
            done = {
                "session_id": payload.session_id,
                "intent": normalize_intent(done_payload.get("intent", "chat"), done_payload.get("action")),
                "action": done_payload.get("action"),
                "blocked": bool(done_payload.get("blocked", False)),
                "crisis_detected": bool(done_payload.get("crisis_detected", False)),
                "safety_violation": bool(done_payload.get("safety_violation", False)),
                "rewritten": bool(done_payload.get("rewritten", False)),
            }
            response_body = dict(done)
            response_body["reply"] = reply
            log_conversation_pair_safe(
                app.state.user_data,
                payload.user_id,
                user_text=payload.message,
                reply=reply,
                intent=done["intent"],
                request_id=rid,
            )
            if executing:
                try:
                    await finish_idempotency(
                        principal, idem, execution_token, 200, response_body
                    )
                except ApiContractError:
                    body = error_body("AGENT_RUNTIME_STATE_UNAVAILABLE", rid)
                    yield "event: error\n" + f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
                    return
            yield f"event: done\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream; charset=utf-8",
            headers={"X-Request-ID": rid, "Cache-Control": "no-cache"},
        )

    @app.post("/v1/speech", responses=error_responses)
    async def v1_speech(
        payload: SpeechRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        authorize(request)
        rid = request_id(request)
        agent: XiaoliaoAgent = app.state.agent
        if agent.tts_client is None:
            return json_response(501, error_body("AGENT_TTS_DISABLED", rid), rid)
        try:
            audio = await run_in_threadpool(agent.tts_client.synthesize, payload.text)
        except Exception:
            return json_response(502, error_body("AGENT_MODEL_UNAVAILABLE", rid), rid)
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"X-Request-ID": rid, "Cache-Control": "no-cache"},
        )

    @app.post("/v1/users/consent", responses=error_responses)
    async def set_user_consent(
        payload: ConsentRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, payload.user_id)
        rid = request_id(request)
        app.state.user_data.get_or_create_user(payload.user_id)
        app.state.user_data.set_consent(
            payload.user_id,
            personalization=payload.personalization,
            sensitive=payload.sensitive,
        )
        app.state.user_data.log_audit(
            payload.user_id,
            action="consent.update",
            resource="consent",
            request_id=rid,
        )
        return json_response(200, {
            "user_id": payload.user_id,
            "personalization": payload.personalization,
            "sensitive": payload.sensitive if payload.personalization else False,
        }, rid)

    @app.get("/v1/me/summary", responses=error_responses)
    async def me_summary(
        request: Request,
        user_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_:@.-]+$"),
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, user_id)
        rid = request_id(request)
        summary = app.state.user_data.summary(user_id)
        return json_response(200, summary, rid)

    def memory_response(record: Any) -> dict[str, Any]:
        return {
            "memory_id": record.memory_id,
            "memory_type": record.memory_type,
            "memory_key": record.memory_key,
            "content": record.content,
            "source_type": record.source_type,
            "supersedes_memory_id": record.supersedes_memory_id,
            "sensitive": record.sensitive,
            "updated_at": record.updated_at.isoformat(),
        }

    @app.get("/v1/me/memories", responses=error_responses)
    async def list_memories(
        request: Request,
        user_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_:@.-]+$"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, user_id)
        rid = request_id(request)
        service = app.state.user_data.memory_service
        records = service.list_current(user_id, limit=limit, offset=offset) if service else []
        return json_response(200, {
            "items": [memory_response(record) for record in records],
            "limit": limit,
            "offset": offset,
        }, rid)

    @app.patch("/v1/me/memories/{memory_id}", responses=error_responses)
    async def correct_memory(
        memory_id: str,
        payload: MemoryCorrectionRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, payload.user_id)
        rid = request_id(request)
        service = app.state.user_data.memory_service
        try:
            if service is None:
                raise KeyError("memory not found")
            record = service.correct(payload.user_id, memory_id, payload.content)
        except KeyError as exc:
            raise ApiContractError(404, "AGENT_MEMORY_NOT_FOUND") from exc
        return json_response(200, memory_response(record), rid)

    @app.delete("/v1/me/memories/{memory_id}", responses=error_responses)
    async def delete_memory(
        memory_id: str,
        request: Request,
        user_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_:@.-]+$"),
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, user_id)
        rid = request_id(request)
        service = app.state.user_data.memory_service
        if service is None or not service.delete_one(user_id, memory_id):
            raise ApiContractError(404, "AGENT_MEMORY_NOT_FOUND")
        return json_response(200, {"status": "deleted"}, rid)

    @app.post(
        "/v1/privacy/delete-request",
        response_model=PrivacyDeletionResponse,
        responses={**error_responses, 202: {"model": PrivacyDeletionResponse}},
    )
    async def privacy_delete_request(
        payload: DeleteUserRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, payload.user_id, allow_deleted=True)
        rid = request_id(request)
        subject_hmac = app.state.reference_service.subject_hmac(payload.user_id)
        report = await app.state.privacy_deletion.delete_user(
            payload.user_id,
            subject_hmac,
            uuid.uuid4().hex,
        )
        body = {
            "status": "deleted" if report.status == "completed" else "redis_pending",
            "subject_hmac": report.subject_hmac,
            "database_counts": report.database_counts,
            "redis_cleaned": report.redis_cleaned,
        }
        return json_response(200 if report.status == "completed" else 202, body, rid)

    @app.post(
        "/v1/action-events",
        response_model=ActionEventResponse,
        responses=error_responses,
    )
    async def action_events(
        payload: ActionEventRequest,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        await authorize_user(request, payload.user_id)
        rid = request_id(request)
        try:
            recommendation, created = await run_in_threadpool(
                app.state.agent.action_service.record_event_with_status,
                ActionEvent(
                    event_id=payload.event_id,
                    recommendation_id=payload.recommendation_id,
                    user_id=payload.user_id,
                    module=payload.module,
                    event_type=payload.event_type,
                    occurred_at=payload.occurred_at,
                    metadata=payload.metadata.model_dump(exclude_none=True),
                    request_summary=payload.summary,
                ),
            )
        except ActionEventConflict as exc:
            raise ApiContractError(409, "AGENT_ACTION_EVENT_CONFLICT") from exc
        except ActionMemoryUnavailable as exc:
            raise ApiContractError(503, "AGENT_MEMORY_UNAVAILABLE") from exc
        except ActionContractError as exc:
            raise ApiContractError(409, "AGENT_ACTION_EVENT_INVALID") from exc
        return json_response(200, {
            "status": "ok",
            "recommendation_id": recommendation.recommendation_id,
            "duplicate": not created,
        }, rid)

    @app.get("/v1/knowledge/search", responses=error_responses)
    async def v1_knowledge_search(
        request: Request,
        q: str,
        top_k: int = 3,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ):
        authorize(request)
        rid = request_id(request)
        top_k = min(max(top_k, 1), 10)
        results = await run_in_threadpool(app.state.agent.kb.search, q[:500], top_k)
        body = {
            "results": [
                {
                    "chunk_id": chunk.chunk_id,
                    "source": chunk.source,
                    "version": chunk.version,
                    "heading_path": list(chunk.heading_path),
                    "score": score,
                }
                for chunk, score in results
            ]
        }
        return json_response(200, body, rid)

    return app


app = create_app()
