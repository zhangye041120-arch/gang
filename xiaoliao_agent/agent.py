from dataclasses import replace
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Generator
import uuid
from datetime import timedelta
import hashlib
import time
import warnings

from pydantic import ValidationError

from .client import ModelClientError, OpenAICompatibleClient
from .config import Settings
from .crisis_repository import (
    MemoryCrisisEventRepository,
    PostgresCrisisEventRepository,
    new_crisis_event,
)
from .actions import (
    ActionAlreadyRecommended,
    ActionContractError,
    ActionService,
    MemoryActionRepository,
    PostgresActionRepository,
)
from .guardrails import (
    CRISIS_FALLBACK,
    FRAUD_FALLBACK,
    GENERIC_FALLBACK,
    MEDICAL_FALLBACK,
    UNSAFE_FALLBACK,
    GuardrailResult,
    precheck,
)
from .live_context import LiveContext, fetch_live_context
from .inspection_repository import MemoryLessonRepository
from .knowledge import KnowledgeBase
from .knowledge_repository import PostgresKnowledgeRepository
from .embeddings import OpenAICompatibleEmbeddingClient
from .lesson_bridge import LessonBridge
from .memory import MemoryCandidate, MemoryService
from .memory_repository import MemoryMemoryRepository, PostgresMemoryRepository
from .notifications import CrisisNotifier
from .quality import InspectionLog, MemoryQualityRepository, PostgresQualityRepository, QualityService
from .prompts import inspector_messages, main_messages, rewrite_messages
from .prompt_registry import get_fallback_reply, get_prompt_spec
from .reminders import MemoryReminderRepository, ReminderService, parse_reminder_request
from .schemas import (
    INSPECTION_ERROR_PATTERNS,
    ActionPayload,
    AgentResult,
    InspectionPayload,
    InspectionResult,
    MainPayload,
    MainResponse,
)

JAVA_INTENTS = frozenset({"chat", "checkin", "game", "exercise", "assessment", "community"})

_ACTION_MODULE_INTENTS = {
    "M1": "checkin",
    "M2": "game",
    "M3": "exercise",
    "M5": "community",
}

_INTENT_ALIASES = {
    "chat": "chat",
    "smalltalk": "chat",
    "emotion_support": "chat",
    "explore_typical_day": "chat",
    "crisis": "chat",
    "safety": "chat",
    "unknown": "chat",
    "checkin": "checkin",
    "emotion_checkin": "checkin",
    "game": "game",
    "games": "game",
    "exercise": "exercise",
    "positive_practice": "exercise",
    "assessment": "assessment",
    "psychological_assessment": "assessment",
    "community": "community",
}

_EXPLICIT_ACTION_BY_MODULE = {
    "M1": {
        "type": "miniprogram",
        "module": "M1",
        "page": "/pages/checkin/index",
        "params": {},
        "reason": "user_requested_checkin",
        "expires_at": None,
    },
    "M2": {
        "type": "miniprogram",
        "module": "M2",
        "page": "/pages/games/index",
        "params": {},
        "reason": "user_requested_game",
        "expires_at": None,
    },
    "M3": {
        "type": "miniprogram",
        "module": "M3",
        "page": "/pages/exercise/index",
        "params": {},
        "reason": "user_requested_exercise",
        "expires_at": None,
    },
    "M5": {
        "type": "miniprogram",
        "module": "M5",
        "page": "/pages/community/index",
        "params": {},
        "reason": "user_requested_community",
        "expires_at": None,
    },
}

_EXPLICIT_INTENT_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (
        re.compile(r"签到|打卡|记(?:一下|一记|个)?(?:今天)?心情|心情打卡"),
        "checkin",
        "M1",
    ),
    (
        re.compile(
            r"脑力游戏|"
            r"(?:我|咱|咱们|我们)?(?:想|要|去|来|能|可以).{0,8}(?:小)?游戏|"
            r"(?:玩|来|去)(?:个|一|一次|一会儿|一下)(?:小)?游戏|"
            r"(?:有什么|有哪些).{0,4}(?:小)?游戏"
        ),
        "game",
        "M2",
    ),
    (
        re.compile(
            r"积极心理练习|心理练习|"
            r"(?:我|咱|咱们|我们)?(?:想|要|去|来|能|可以).{0,8}(?:做|练).{0,2}练习|"
            r"(?:做|来)(?:个|一|一次|一下)(?:心理|积极)?练习|"
            r"练习一下|三件好事|感恩留言"
        ),
        "exercise",
        "M3",
    ),
    (
        re.compile(
            r"心理测评|"
            r"(?:我|咱|咱们|我们)?(?:想|要|去|来|能|可以).{0,6}测评|"
            r"(?:做个|做一下)测评|测一测(?:心理)?"
        ),
        "assessment",
        None,
    ),
    (
        re.compile(
            r"兴趣圈|找朋友(?:聊聊天|聊天)|发(?:个)?帖子|发帖|"
            r"(?:我|咱|咱们|我们)?(?:想|要|去|来|能|可以).{0,6}(?:社区|圈子)|"
            r"(?:去|逛)社区"
        ),
        "community",
        "M5",
    ),
)

_IMPLICIT_INTENT_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (
        re.compile(
            r"心情不太好.{0,12}记|"
            r"把(?:今天)?(?:心情|感觉).{0,6}(?:记|写)(?:一下|下来)|"
            r"(?:想|要).{0,6}(?:记|写)(?:一下|下来).{0,6}(?:心情|感觉)"
        ),
        "checkin",
        "M1",
    ),
    (
        re.compile(r"练练脑子|练练脑|动动脑|健脑|出去走一走|出去走走|散步|想练一练|练一练"),
        "exercise",
        "M3",
    ),
    (
        re.compile(
            r"一个人在家有点闷|闷得慌|心里闷|"
            r"想找人(?:说话|聊聊天|聊聊|说说话)|想聊聊天|想找人陪我"
        ),
        "community",
        "M5",
    ),
    (
        re.compile(r"想测测自己|测测(?:我的)?(?:心情|状态|心理)|心理状态怎么样"),
        "assessment",
        None,
    ),
)

_MODEL_INTENT_MODULES = {
    "checkin": "M1",
    "game": "M2",
    "exercise": "M3",
    "community": "M5",
}

_AGE_PATTERNS = (
    re.compile(r"我(?:今年|现在|已经)?\s*(\d{1,3})\s*岁(?![的岁])"),
    re.compile(r"我今年\s*(\d{1,3})(?=[，。！？\s]|$)"),
    re.compile(r"我已经\s*(\d{1,3})\s*岁了?"),
)


def to_java_intent(raw_intent: str, action: dict[str, Any] | None = None) -> str:
    module = str((action or {}).get("module", "")).upper()
    if module in _ACTION_MODULE_INTENTS:
        return _ACTION_MODULE_INTENTS[module]
    return _INTENT_ALIASES.get((raw_intent or "").strip().lower(), "chat")


def resolve_explicit_intent(user_text: str) -> tuple[str, dict[str, Any] | None] | None:
    """Deterministically recognize module requests from the user message."""
    text = (user_text or "").strip()
    if not text:
        return None
    for rules in (_EXPLICIT_INTENT_RULES, _IMPLICIT_INTENT_RULES):
        for pattern, intent, module in rules:
            if pattern.search(text):
                action = _EXPLICIT_ACTION_BY_MODULE.get(module)
                return intent, dict(action) if action else None
    return None


def apply_explicit_intent(user_text: str, candidate: MainResponse) -> MainResponse:
    """Overwrite chat-only model output when the user requests a module."""
    resolved = resolve_explicit_intent(user_text)
    if resolved is not None:
        intent, action = resolved
        return replace(candidate, intent=intent, action=action, errors=[])
    java_intent = to_java_intent(candidate.intent, None)
    module = _MODEL_INTENT_MODULES.get(java_intent)
    if module and candidate.action is None:
        return replace(candidate, action=dict(_EXPLICIT_ACTION_BY_MODULE[module]), errors=[])
    return candidate


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError as exc:
        raise ValueError("模型没有返回严格 JSON 对象") from exc
    raise ValueError("模型 JSON 不是对象")


class AgentInvalidResponseError(ValueError):
    code = "AGENT_INVALID_JSON"


def parse_main(text: str) -> MainResponse:
    try:
        data = parse_json_object(text)
        payload = MainPayload.model_validate(data)
    except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise AgentInvalidResponseError("主模型返回不符合 JSON 合同") from exc
    reply = payload.reply.strip()
    intent = payload.intent.strip()
    if not reply or not intent:
        raise AgentInvalidResponseError("主模型返回空字段")
    action = None
    errors: list[str] = []
    if payload.action is not None:
        try:
            action = ActionPayload.model_validate(payload.action).model_dump()
        except ValidationError:
            errors.append("AGENT_INVALID_ACTION")
    return MainResponse(
        reply=reply,
        intent=intent,
        action=action,
        risk_hint=payload.risk_hint,
        raw=text,
        errors=errors,
    )


class AgentInvalidInspectionError(ValueError):
    code = "AGENT_INVALID_INSPECTION"


def parse_inspection(text: str) -> InspectionResult:
    try:
        data = parse_json_object(text)
        error_pattern = data.get("error_pattern")
        if isinstance(error_pattern, str) and error_pattern not in INSPECTION_ERROR_PATTERNS:
            data["error_pattern"] = "unknown"
        payload = InspectionPayload.model_validate(data)
    except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise AgentInvalidInspectionError("Inspector 返回不符合严格合同") from exc
    return InspectionResult(
        crisis_detected=payload.crisis_detected,
        safety_violation=payload.safety_violation,
        intent_accurate=payload.intent_accurate,
        age_appropriate=payload.age_appropriate,
        cbt_appropriate=payload.cbt_appropriate,
        issues=payload.issues,
        suggestion=payload.suggestion,
        error_pattern=payload.error_pattern,
        lesson=payload.lesson,
        raw=text,
    )


def _db_reachable(database_url: str, timeout: int = 5) -> bool:
    """Return True if the database accepts a connection within *timeout* seconds."""
    if not database_url:
        return False
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=timeout):
            return True
    except Exception:
        return False


class XiaoliaoAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        knowledge_base: KnowledgeBase | None = None,
        main_client: Any | None = None,
        inspector_client: Any | None = None,
        inspector_escalation_client: Any | None = None,
        live_context_provider: Any | None = None,
        reminder_service: Any | None = None,
        lesson_repository: Any | None = None,
        crisis_repository: Any | None = None,
        crisis_notifier: Any | None = None,
        memory_service: Any | None = None,
        action_service: Any | None = None,
        quality_service: Any | None = None,
    ):
        self.settings = settings or Settings.from_env()
        if self.settings.knowledge_database_url and not _db_reachable(self.settings.knowledge_database_url):
            hidden = self.settings.knowledge_database_url
            if "@" in hidden:
                hidden = hidden.split("@")[-1]
            warnings.warn(
                f"数据库 {hidden} 不可达，回退到内存模式。请启动 Docker 或检查 KNOWLEDGE_DATABASE_URL。",
                stacklevel=2,
            )
            self.settings = replace(self.settings, knowledge_database_url="")
        for prompt_id in ("main-agent", "inspector", "rewrite"):
            get_prompt_spec(prompt_id, self.settings.prompt_version)
        # Shared embedding client — reused by KB, memory, and lesson bridge
        self._embed_client = None
        if self.settings.embedding_provider.lower() == "qwen" and self.settings.embedding_model:
            self._embed_client = OpenAICompatibleEmbeddingClient(
                self.settings.qwen_base_url,
                self.settings.qwen_api_key,
                self.settings.embedding_model,
                dimension=self.settings.embedding_dimension,
                timeout=self.settings.timeout_seconds,
            )

        if knowledge_base is not None:
            self.kb = knowledge_base
        else:
            vector_search = None
            if self.settings.knowledge_database_url and self._embed_client is not None:
                vector_repository = PostgresKnowledgeRepository(self.settings.knowledge_database_url)

                def vector_search(query: str, top_k: int):
                    vector = self._embed_client.embed([query])[0]
                    return vector_repository.vector_search(vector, top_k, version=self.settings.knowledge_version)

            self.kb = KnowledgeBase.from_files(
                self.settings.knowledge_path,
                self.settings.lessons_path,
                self.settings.elder_scenarios_path,
                self.settings.regional_resources_path,
                self.settings.health_knowledge_path,
                self.settings.fraud_knowledge_path,
                self.settings.leisure_knowledge_path,
                version=self.settings.knowledge_version,
                vector_search=vector_search,
            )
        if main_client is None or inspector_client is None:
            self.settings.validate_live()
        self.main_client = main_client or OpenAICompatibleClient(
            self.settings.deepseek_base_url, self.settings.deepseek_api_key,
            self.settings.deepseek_model, self.settings,
        )
        self.inspector_client = inspector_client or OpenAICompatibleClient(
            self.settings.qwen_base_url, self.settings.qwen_api_key,
            self.settings.qwen_model, self.settings,
            max_tokens=self.settings.inspector_max_tokens,
            enable_thinking=self.settings.inspector_enable_thinking,
        )
        self._inspector_escalation_client = inspector_escalation_client
        self.live_context_provider = live_context_provider or (
            lambda text: fetch_live_context(text, self.settings)
        )
        self.reminder_service = reminder_service or ReminderService(MemoryReminderRepository())
        self.lesson_repository = lesson_repository or MemoryLessonRepository()
        if crisis_repository is not None:
            self.crisis_repository = crisis_repository
        elif self.settings.knowledge_database_url:
            self.crisis_repository = PostgresCrisisEventRepository(self.settings.knowledge_database_url)
        else:
            self.crisis_repository = MemoryCrisisEventRepository()
        self.crisis_notifier = crisis_notifier or CrisisNotifier(
            route=self.settings.crisis_route,
            max_attempts=self.settings.crisis_notification_attempts,
        )
        if memory_service is not None:
            self.memory_service = memory_service
        elif self.settings.knowledge_database_url:
            self.memory_service = MemoryService(
                PostgresMemoryRepository(self.settings.knowledge_database_url),
                embed_client=self._embed_client if self.settings.memory_vector_search_enabled else None,
                confidence_threshold=self.settings.memory_confidence_threshold,
                default_limit=self.settings.memory_max_items,
                default_max_chars=self.settings.memory_max_chars,
                vector_min_score=self.settings.memory_vector_min_score,
                vector_search_fallback=True,
            )
        else:
            self.memory_service = MemoryService(
                MemoryMemoryRepository(),
                confidence_threshold=self.settings.memory_confidence_threshold,
                default_limit=self.settings.memory_max_items,
                default_max_chars=self.settings.memory_max_chars,
            )
        if action_service is not None:
            self.action_service = action_service
        elif self.settings.knowledge_database_url:
            self.action_service = ActionService(
                PostgresActionRepository(self.settings.knowledge_database_url),
                memory_service=self.memory_service,
                decline_cooldown=timedelta(hours=self.settings.action_decline_cooldown_hours),
            )
        else:
            self.action_service = ActionService(
                MemoryActionRepository(),
                memory_service=self.memory_service,
                decline_cooldown=timedelta(hours=self.settings.action_decline_cooldown_hours),
            )
        if quality_service is not None:
            self.quality_service = quality_service
        elif self.settings.knowledge_database_url:
            self.quality_service = QualityService(PostgresQualityRepository(self.settings.knowledge_database_url))
        else:
            self.quality_service = QualityService(MemoryQualityRepository())
        # Lesson bridge — feeds approved operational lessons back into RAG
        self.lesson_bridge = LessonBridge(
            self.quality_service.repository,
            embed_client=self._embed_client,
            top_k=self.settings.lesson_search_top_k,
        )

    def _generate(
        self,
        user_text: str,
        context: str,
        history: list[dict[str, str]],
        memory_context: str,
    ) -> MainResponse:
        raw = self.main_client.chat(
            main_messages(
                user_text,
                context,
                history,
                self.settings.prompt_version,
                memory_context,
            ),
            json_mode=True,
        )
        return parse_main(raw)

    def _generate_stream(
        self,
        user_text: str,
        context: str,
        history: list[dict[str, str]],
        memory_context: str,
    ) -> Generator[str, None, MainResponse]:
        """Stream tokens from the main model.  The caller iterates over tokens,
        and the final ``.value`` attribute of the *StopIteration* holds the
        parsed ``MainResponse`` (PEP 479 – replace ``StopIteration.value``
        with explicit return in the caller)."""
        if not hasattr(self.main_client, "chat_stream"):
            yield ""
            return self._generate(user_text, context, history, memory_context)

        accumulated = ""
        try:
            for token in self.main_client.chat_stream(
                main_messages(user_text, context, history, self.settings.prompt_version, memory_context),
                json_mode=True,
            ):
                if token.startswith("__XLM_USAGE__:"):
                    break
                accumulated += token
                yield token
        except ModelClientError:
            raise
        # Try to parse whatever we accumulated
        main_resp = parse_main(accumulated)
        return main_resp

    def _fetch_rag(
        self,
        user_text: str,
        user_id: str,
        personalization: bool,
        memory_context: str,
    ) -> tuple[str, str, list[dict[str, object]]]:
        """Return (combined_context, effective_memory, sources).

        When ``rag_parallel_enabled`` is True, CBT knowledge, lessons, user
        memory, and any live factual lookup are fetched concurrently on a
        thread pool.
        """
        reminder_context, reminder_source = self._reminder_context(user_text, user_id)
        if not self.settings.rag_parallel_enabled:
            stored_memory = self.memory_service.get_context(user_id, query=user_text) if personalization else ""
            effective_memory = "\n".join(part for part in (memory_context, stored_memory) if part)
            kb_context, sources = self.kb.context(user_text, top_k=self.settings.top_k)
            lesson_context, lesson_sources = self.lesson_bridge.context(user_text)
            combined_context = "\n\n".join(part for part in (kb_context, lesson_context) if part)
            if reminder_context:
                combined_context = "\n\n".join(part for part in (reminder_context, combined_context) if part)
                sources.append(reminder_source)
            live = self.live_context_provider(user_text)
            if live is not None and live.content:
                combined_context = "\n\n".join(
                    part for part in (combined_context, f"[{live.section}]\n{live.content}") if part
                )
                sources.extend(live.sources)
            return combined_context, effective_memory, sources + lesson_sources

        # Parallel fetch
        effective_memory = memory_context
        combined_context = ""
        sources: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            fut_kb = executor.submit(self.kb.context, user_text, self.settings.top_k)
            fut_lesson = executor.submit(self.lesson_bridge.context, user_text)
            fut_mem = executor.submit(self.memory_service.get_context, user_id, query=user_text) if personalization else None
            fut_live = executor.submit(self.live_context_provider, user_text)

            for fut in as_completed([f for f in (fut_kb, fut_lesson, fut_mem, fut_live) if f is not None]):
                if fut is fut_kb:
                    kb_context, kb_sources = fut.result()
                    sources.extend(kb_sources)
                    if kb_context:
                        combined_context = "\n\n".join(
                            part for part in (kb_context, combined_context) if part
                        )
                elif fut is fut_lesson:
                    lesson_context, lesson_sources = fut.result()
                    sources.extend(lesson_sources)
                    if lesson_context:
                        combined_context = "\n\n".join(part for part in (combined_context, lesson_context) if part)
                elif fut is fut_mem:
                    stored = fut.result()
                    effective_memory = "\n".join(part for part in (memory_context, stored) if part)
                elif fut is fut_live:
                    live = fut.result()
                    if live is not None and live.content:
                        combined_context = "\n\n".join(
                            part for part in (combined_context, f"[{live.section}]\n{live.content}") if part
                        )
                        sources.extend(live.sources)

        if reminder_context:
            combined_context = "\n\n".join(part for part in (reminder_context, combined_context) if part)
            sources.append(reminder_source)
        return combined_context, effective_memory, sources

    def _reminder_context(
        self,
        user_text: str,
        user_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        if not self.settings.reminders_enabled:
            return "", None
        request = parse_reminder_request(user_text)
        if request is None:
            return "", None
        reminder, _created = self.reminder_service.create(
            user_id,
            request,
            source_request_id=getattr(self.main_client, "last_request_id", "") or uuid.uuid4().hex,
        )
        content = (
            f"[提醒]\n已为您记录提醒：{reminder.content}（"
            f"{reminder.due_at.strftime('%m月%d日 %H:%M')}"
            f"{'，每天重复' if reminder.recurring else ''}）。"
            "提醒只做本地记录，不会自动打电话或修改系统。"
        )
        return content, {"source": "reminder:created", "title": "提醒设置", "content": content}

    def _persist_profile_memory(
        self,
        user_text: str,
        user_id: str,
        request_id: str,
    ) -> None:
        """Save explicitly stated age facts into authorized long-term memory."""
        if not user_id.strip():
            return
        age_match = None
        for pattern in _AGE_PATTERNS:
            age_match = pattern.search(user_text)
            if age_match:
                break
        if age_match is None:
            return
        age = int(age_match.group(1))
        if not 0 < age < 130:
            return
        key = "用户年龄："
        content = f"{key}{age}岁"
        try:
            existing = [
                record
                for record in self.memory_service.view(user_id)
                if record.memory_type == "profile" and record.content.startswith(key)
            ]
            if existing:
                self.memory_service.correct(user_id, existing[0].memory_id, content)
            else:
                self.memory_service.save_candidate(
                    user_id,
                    MemoryCandidate(
                        memory_type="profile",
                        content=content,
                        confidence=1.0,
                        source_message_id=request_id or uuid.uuid4().hex,
                        consent_scope="personalization",
                        explicitly_stated=True,
                    ),
                )
        except Exception:
            # Memory extraction must never break the conversation.
            pass

    def _fallback_result(
        self,
        error_code: str,
        *,
        request_id: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        rewritten: bool = False,
        inspection: InspectionResult | None = None,
    ) -> AgentResult:
        inspection = inspection or InspectionResult(
            issues=["Agent 未产生可安全发送的结构化回复"],
            error_pattern="invalid_response",
        )
        return AgentResult(
            get_fallback_reply(self.settings.prompt_version),
            "chat",
            None,
            False,
            False,
            False,
            rewritten,
            inspection,
            sources or [],
            error_code=error_code,
            request_id=request_id,
            main_model=self.settings.deepseek_model,
            prompt_version=self.settings.prompt_version,
        )

    def _inspector_escalation_client_instance(self) -> OpenAICompatibleClient:
        if self._inspector_escalation_client is None and isinstance(self.inspector_client, OpenAICompatibleClient):
            self._inspector_escalation_client = OpenAICompatibleClient(
                self.settings.qwen_base_url,
                self.settings.qwen_api_key,
                self.settings.qwen_model,
                self.settings,
                max_tokens=self.settings.inspector_max_tokens,
                enable_thinking=True,
            )
        return self._inspector_escalation_client or self.inspector_client

    def _run_inspector(self, user_text: str, candidate: MainResponse, *, thinking: bool = False) -> InspectionResult:
        client = self._inspector_escalation_client_instance() if thinking else self.inspector_client
        try:
            raw = client.chat(
                inspector_messages(user_text, candidate.reply, candidate.intent, self.settings.prompt_version),
                json_mode=True,
            )
            inspection = parse_inspection(raw)
        except (AgentInvalidInspectionError, ModelClientError):
            return InspectionResult(
                safety_violation=True,
                issues=["副 Agent 返回格式无法解析，按不安全处理"],
                suggestion="返回安全兜底话术",
                error_pattern="invalid_response",
            )
        if inspection.lesson.strip():
            self.lesson_repository.add_candidate(
                inspection.lesson.strip(),
                inspection.error_pattern,
                self.settings.prompt_version,
            )
        return inspection

    def _inspect(self, user_text: str, candidate: MainResponse) -> InspectionResult:
        return self._run_inspector(user_text, candidate, thinking=False)

    def _should_escalate(self, candidate: MainResponse, inspection: InspectionResult) -> bool:
        if (
            not self.settings.inspector_escalate_on_issues
            or inspection.hard_blocked
            or (self._inspector_escalation_client is None and not isinstance(self.inspector_client, OpenAICompatibleClient))
        ):
            return False
        risk_hint = str(candidate.risk_hint or "").strip().lower()
        return bool(
            inspection.issues
            or inspection.soft_failed
            or inspection.error_pattern != "none"
            or (risk_hint not in {"", "none"})
        )

    def _guardrail_result(
        self,
        risk: GuardrailResult,
        *,
        user_id: str,
        session_id: str,
        sources: list[dict[str, Any]] | None = None,
        rewritten: bool = False,
    ) -> AgentResult:
        replies = {
            "crisis": CRISIS_FALLBACK,
            "medical_boundary": MEDICAL_FALLBACK,
            "unsafe_content": UNSAFE_FALLBACK,
        }
        reply = replies[risk.risk_category]
        if risk.risk_category == "unsafe_content" and any(
            str(rule).startswith("F") for rule in (risk.rule_ids or [])
        ):
            reply = FRAUD_FALLBACK
        alerts: list[str] = []
        event_id = None
        if risk.risk_category == "crisis":
            event = new_crisis_event(
                user_id=user_id,
                session_id=session_id,
                evidence_code=(risk.rule_ids or ["C_UNKNOWN"])[0],
                route=self.settings.crisis_route,
                prompt_version=self.settings.prompt_version,
                response_version=risk.response_version,
            )
            event_id = event.event_id
            try:
                self.crisis_repository.add(event)
            except Exception:
                alerts.append("crisis_event_persist_failed")
            alerts.extend(self.crisis_notifier.notify(event))
        inspection = InspectionResult(
            crisis_detected=risk.crisis_detected,
            safety_violation=risk.safety_violation,
            issues=risk.rule_ids or [],
            error_pattern=risk.risk_category,
        )
        return AgentResult(
            reply,
            "crisis" if risk.crisis_detected else "safety",
            None,
            True,
            risk.crisis_detected,
            risk.safety_violation,
            rewritten,
            inspection,
            sources or [],
            main_model=self.settings.deepseek_model,
            prompt_version=self.settings.prompt_version,
            risk_category=risk.risk_category,
            crisis_event_id=event_id,
            alerts=alerts,
            response_version=risk.response_version,
        )

    def chat(
        self,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        memory_context: str = "",
        user_id: str = "anonymous",
        session_id: str = "",
        personalization: bool = False,
        message_id: str = "",
        request_id: str = "",
    ) -> AgentResult:
        started = time.perf_counter()
        if hasattr(self.main_client, "last_usage"):
            self.main_client.last_usage = None
        if hasattr(self.inspector_client, "last_usage"):
            self.inspector_client.last_usage = None
        escalation = getattr(self, "_inspector_escalation_client", None)
        if escalation is not None and hasattr(escalation, "last_usage"):
            escalation.last_usage = None
        result = self._chat_impl(
            user_text,
            history,
            memory_context,
            user_id,
            session_id,
            personalization,
        )
        result.stage_latencies["total_ms"] = max(0, int((time.perf_counter() - started) * 1000))
        result.request_id = request_id or result.request_id or uuid.uuid4().hex
        if personalization and not result.blocked and not result.error_code:
            self._persist_profile_memory(user_text, user_id, result.request_id)
        usage_items = [
            getattr(self.main_client, "last_usage", None),
            getattr(self.inspector_client, "last_usage", None),
        ]
        if escalation is not None and escalation is not self.inspector_client:
            usage_items.append(getattr(escalation, "last_usage", None))

        def usage_total(field: str) -> int | None:
            values = [item.get(field) for item in usage_items if isinstance(item, dict) and isinstance(item.get(field), int)]
            return sum(values) if values else None

        lesson_ref = None
        if result.inspection.lesson.strip():
            try:
                lesson = self.quality_service.repository.add_lesson(
                    result.inspection.lesson.strip(),
                    result.inspection.error_pattern if result.inspection.error_pattern in INSPECTION_ERROR_PATTERNS else "unknown",
                    self.settings.prompt_version,
                )
                lesson_ref = lesson.lesson_id
            except Exception:
                result.alerts.append("lesson_write_failed")
        error_pattern = result.inspection.error_pattern
        if error_pattern not in INSPECTION_ERROR_PATTERNS:
            error_pattern = "unknown"
        log = InspectionLog(
            request_id=result.request_id,
            message_id=message_id or f"msg-{result.request_id}",
            user_hash=hashlib.sha256(f"{self.settings.quality_hash_salt}:{user_id}".encode("utf-8")).hexdigest(),
            candidate_reply_ref="sha256:" + hashlib.sha256(result.reply.encode("utf-8")).hexdigest(),
            crisis_detected=result.crisis_detected,
            safety_violation=result.safety_violation,
            intent_accurate=result.inspection.intent_accurate,
            age_appropriate=result.inspection.age_appropriate,
            cbt_appropriate=result.inspection.cbt_appropriate,
            issues=list(result.inspection.issues),
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            main_model=self.settings.deepseek_model,
            inspector_model=self.settings.qwen_model,
            prompt_version=self.settings.prompt_version,
            input_tokens=usage_total("input_tokens"),
            output_tokens=usage_total("output_tokens"),
            total_tokens=usage_total("total_tokens"),
            cost=None,
            error_pattern=error_pattern,
            lesson_ref=lesson_ref,
        )
        result.alerts.extend(self.quality_service.write_log(log))
        return result

    def chat_stream(
        self,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        memory_context: str = "",
        user_id: str = "anonymous",
        session_id: str = "",
        personalization: bool = False,
    ) -> Generator[dict[str, Any], None, None]:
        """Safe buffered stream: reply tokens are emitted only after inspection.

        Yields:
            {"type": "token", "content": "..."} — reply text fragments
            {"type": "done", ...}            — final metadata + inspection
            {"type": "corrected", "reply": "..."}  — reply was rewritten
        """
        started = time.perf_counter()
        if hasattr(self.main_client, "last_usage"):
            self.main_client.last_usage = None
        if hasattr(self.inspector_client, "last_usage"):
            self.inspector_client.last_usage = None

        history = history or []

        # ── deterministic precheck ──────────────────────────────
        quick_input = precheck(user_text)
        if quick_input.risk_category != "normal":
            result = self._guardrail_result(
                quick_input, user_id=user_id, session_id=session_id,
            )
            yield {"type": "token", "content": result.reply}
            yield {"type": "done", "blocked": True, "crisis_detected": result.crisis_detected,
                   "safety_violation": result.safety_violation, "latency_ms": 0}
            return

        # ── parallel RAG ────────────────────────────────────────
        combined_context, effective_memory, sources = self._fetch_rag(
            user_text, user_id, personalization, memory_context,
        )

        # ── stream main agent ────────────────────────────────────
        accumulated = ""
        main_error: str | None = None
        try:
            gen = self._generate_stream(user_text, combined_context, history, effective_memory)
            while True:
                try:
                    token = next(gen)
                    accumulated += token
                except StopIteration as exc:
                    candidate = exc.value  # MainResponse returned by _generate_stream
                    break
        except AgentInvalidResponseError:
            main_error = "AGENT_INVALID_JSON"
        except ModelClientError as exc:
            main_error = exc.code

        if main_error:
            fallback = self._fallback_result(main_error, sources=sources)
            yield {"type": "token", "content": fallback.reply}
            latency = int((time.perf_counter() - started) * 1000)
            yield {"type": "done", "blocked": False, "crisis_detected": False,
                   "safety_violation": False, "rewritten": False,
                   "latency_ms": latency, "error_code": main_error}
            return

        # ── deterministic postcheck ──────────────────────────────
        if 'candidate' not in dir():
            candidate = parse_main(accumulated)
        candidate = apply_explicit_intent(user_text, candidate)
        quick = precheck(user_text, candidate.reply)
        if quick.risk_category != "normal":
            result = self._guardrail_result(
                quick, user_id=user_id, session_id=session_id, sources=sources,
            )
            yield {"type": "corrected", "reply": result.reply}
            latency = int((time.perf_counter() - started) * 1000)
            yield {"type": "done", "blocked": True,
                   "crisis_detected": result.crisis_detected,
                   "safety_violation": result.safety_violation,
                   "rewritten": False, "latency_ms": latency}
            return

        # ── inspector ────────────────────────────────────────────
        inspection = self._inspect(user_text, candidate)
        if self._should_escalate(candidate, inspection):
            inspection = self._run_inspector(user_text, candidate, thinking=True)
        rewritten = False
        final_reply = candidate.reply

        if inspection.hard_blocked:
            risk = GuardrailResult(
                "crisis" if inspection.crisis_detected else "medical_boundary",
                ["I001" if inspection.crisis_detected else "I002"],
                inspection.crisis_detected, inspection.safety_violation,
                response_version="crisis-v1.0.0" if inspection.crisis_detected else "medical-v1.0.0",
            )
            result = self._guardrail_result(
                risk, user_id=user_id, session_id=session_id, sources=sources,
            )
            yield {"type": "corrected", "reply": result.reply}
            latency = int((time.perf_counter() - started) * 1000)
            yield {"type": "done", "blocked": True,
                   "crisis_detected": result.crisis_detected,
                   "safety_violation": result.safety_violation,
                   "rewritten": False, "latency_ms": latency}
            return

        if inspection.soft_failed:
            rewritten = True
            try:
                candidate = parse_main(self.main_client.chat(
                    rewrite_messages(
                        user_text, candidate.reply, inspection,
                        combined_context, self.settings.prompt_version,
                    ),
                    json_mode=True,
                ))
                candidate = apply_explicit_intent(user_text, candidate)
            except (AgentInvalidResponseError, ModelClientError):
                fallback = self._fallback_result(
                    "AGENT_INSPECTION_FAILED", sources=sources, rewritten=True, inspection=inspection,
                )
                yield {"type": "corrected", "reply": fallback.reply}
                latency = int((time.perf_counter() - started) * 1000)
                yield {"type": "done", "blocked": False, "crisis_detected": False,
                       "safety_violation": False, "rewritten": True, "latency_ms": latency}
                return
            second_quick = precheck(user_text, candidate.reply)
            if second_quick.risk_category != "normal":
                result = self._guardrail_result(
                    second_quick, user_id=user_id, session_id=session_id,
                    sources=sources, rewritten=True,
                )
                yield {"type": "corrected", "reply": result.reply}
                latency = int((time.perf_counter() - started) * 1000)
                yield {"type": "done", "blocked": True,
                       "crisis_detected": result.crisis_detected,
                       "safety_violation": result.safety_violation,
                       "rewritten": True, "latency_ms": latency}
                return
            inspection = self._inspect(user_text, candidate)
            if self._should_escalate(candidate, inspection):
                inspection = self._run_inspector(user_text, candidate, thinking=True)
            if inspection.hard_blocked:
                risk = GuardrailResult(
                    "crisis" if inspection.crisis_detected else "medical_boundary",
                    ["I001" if inspection.crisis_detected else "I002"],
                    inspection.crisis_detected, inspection.safety_violation,
                )
                result = self._guardrail_result(
                    risk, user_id=user_id, session_id=session_id,
                    sources=sources, rewritten=True,
                )
                yield {"type": "corrected", "reply": result.reply}
                latency = int((time.perf_counter() - started) * 1000)
                yield {"type": "done", "blocked": True,
                       "crisis_detected": result.crisis_detected,
                       "safety_violation": result.safety_violation,
                       "rewritten": True, "latency_ms": latency}
                return
            if inspection.soft_failed:
                fallback = self._fallback_result(
                    "AGENT_INSPECTION_FAILED", sources=sources, rewritten=True, inspection=inspection,
                )
                yield {"type": "corrected", "reply": fallback.reply}
                latency = int((time.perf_counter() - started) * 1000)
                yield {"type": "done", "blocked": False, "crisis_detected": False,
                       "safety_violation": False, "rewritten": True, "latency_ms": latency}
                return
            final_reply = candidate.reply
            # Stream the rewritten reply
            yield {"type": "corrected", "reply": final_reply}

        chunks = [final_reply[i:i + 4] for i in range(0, len(final_reply), 4)] or [""]
        for chunk in chunks:
            yield {"type": "token", "content": chunk}

        # ── action recommendation ────────────────────────────────
        final_action = candidate.action
        recommendation_id = None
        if final_action is not None:
            try:
                explicit_request = resolve_explicit_intent(user_text) is not None
                recommendation = self.action_service.recommend(
                    user_id, session_id, final_action,
                    source_message_id=getattr(self.main_client, "last_request_id", None) or uuid.uuid4().hex,
                    deduplicate=not explicit_request,
                )
                recommendation_id = recommendation.recommendation_id
            except ActionAlreadyRecommended:
                final_action = None
            except ActionContractError:
                final_action = None

        if personalization:
            self._persist_profile_memory(
                user_text,
                user_id,
                getattr(self.main_client, "last_request_id", None) or uuid.uuid4().hex,
            )
        latency = int((time.perf_counter() - started) * 1000)
        yield {
            "type": "done",
            "blocked": False,
            "crisis_detected": inspection.crisis_detected,
            "safety_violation": inspection.safety_violation,
            "rewritten": rewritten,
            "latency_ms": latency,
            "intent": to_java_intent(candidate.intent, final_action),
            "action": final_action,
            "recommendation_id": recommendation_id,
            "request_id": getattr(self.main_client, "last_request_id", None),
        }

    @staticmethod
    def _with_stages(result: AgentResult, stages: dict[str, int]) -> AgentResult:
        result.stage_latencies = dict(stages)
        return result

    def _chat_impl(
        self,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        memory_context: str = "",
        user_id: str = "anonymous",
        session_id: str = "",
        personalization: bool = False,
    ) -> AgentResult:
        history = history or []
        stages: dict[str, int] = {}
        start = time.perf_counter()
        quick_input = precheck(user_text)
        stages["precheck_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        if quick_input.risk_category != "normal":
            return self._with_stages(
                self._guardrail_result(quick_input, user_id=user_id, session_id=session_id),
                stages,
            )

        start = time.perf_counter()
        combined_context, effective_memory, sources = self._fetch_rag(
            user_text, user_id, personalization, memory_context,
        )
        stages["rag_ms"] = max(0, int((time.perf_counter() - start) * 1000))

        start = time.perf_counter()
        try:
            candidate = self._generate(user_text, combined_context, history, effective_memory)
        except AgentInvalidResponseError:
            stages["main_ms"] = max(0, int((time.perf_counter() - start) * 1000))
            return self._with_stages(
                self._fallback_result(
                    "AGENT_INVALID_JSON",
                    request_id=getattr(self.main_client, "last_request_id", None),
                    sources=sources,
                ),
                stages,
            )
        except ModelClientError as exc:
            stages["main_ms"] = max(0, int((time.perf_counter() - start) * 1000))
            return self._with_stages(
                self._fallback_result(exc.code, request_id=exc.request_id, sources=sources),
                stages,
            )
        stages["main_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        candidate = apply_explicit_intent(user_text, candidate)

        start = time.perf_counter()
        quick = precheck(user_text, candidate.reply)
        stages["postcheck_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        if quick.risk_category != "normal":
            return self._with_stages(
                self._guardrail_result(
                    quick, user_id=user_id, session_id=session_id, sources=sources,
                ),
                stages,
            )

        start = time.perf_counter()
        inspection = self._inspect(user_text, candidate)
        stages["inspector_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        if self._should_escalate(candidate, inspection):
            start = time.perf_counter()
            inspection = self._run_inspector(user_text, candidate, thinking=True)
            stages["escalated_inspector_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        if inspection.hard_blocked:
            risk = GuardrailResult(
                "crisis" if inspection.crisis_detected else "medical_boundary",
                ["I001" if inspection.crisis_detected else "I002"],
                inspection.crisis_detected,
                inspection.safety_violation,
                response_version="crisis-v1.0.0" if inspection.crisis_detected else "medical-v1.0.0",
            )
            return self._with_stages(
                self._guardrail_result(risk, user_id=user_id, session_id=session_id, sources=sources),
                stages,
            )

        rewritten = False
        if inspection.soft_failed:
            rewritten = True
            start = time.perf_counter()
            try:
                candidate = parse_main(self.main_client.chat(
                    rewrite_messages(
                        user_text,
                        candidate.reply,
                        inspection,
                        combined_context,
                        self.settings.prompt_version,
                    ),
                    json_mode=True,
                ))
                candidate = apply_explicit_intent(user_text, candidate)
            except AgentInvalidResponseError:
                stages["rewrite_ms"] = max(0, int((time.perf_counter() - start) * 1000))
                return self._with_stages(
                    self._fallback_result(
                        "AGENT_INVALID_JSON",
                        request_id=getattr(self.main_client, "last_request_id", None),
                        sources=sources,
                        rewritten=True,
                    ),
                    stages,
                )
            except ModelClientError as exc:
                stages["rewrite_ms"] = max(0, int((time.perf_counter() - start) * 1000))
                return self._with_stages(
                    self._fallback_result(
                        exc.code,
                        request_id=exc.request_id,
                        sources=sources,
                        rewritten=True,
                    ),
                    stages,
                )
            stages["rewrite_ms"] = max(0, int((time.perf_counter() - start) * 1000))

            start = time.perf_counter()
            second_quick = precheck(user_text, candidate.reply)
            stages["second_postcheck_ms"] = max(0, int((time.perf_counter() - start) * 1000))
            if second_quick.risk_category != "normal":
                return self._with_stages(
                    self._guardrail_result(
                        second_quick,
                        user_id=user_id,
                        session_id=session_id,
                        sources=sources,
                        rewritten=True,
                    ),
                    stages,
                )

            start = time.perf_counter()
            inspection = self._inspect(user_text, candidate)
            stages["second_inspector_ms"] = max(0, int((time.perf_counter() - start) * 1000))
            if self._should_escalate(candidate, inspection):
                start = time.perf_counter()
                inspection = self._run_inspector(user_text, candidate, thinking=True)
                stages["second_escalated_inspector_ms"] = max(0, int((time.perf_counter() - start) * 1000))
            if inspection.hard_blocked:
                risk = GuardrailResult(
                    "crisis" if inspection.crisis_detected else "medical_boundary",
                    ["I001" if inspection.crisis_detected else "I002"],
                    inspection.crisis_detected,
                    inspection.safety_violation,
                    response_version="crisis-v1.0.0" if inspection.crisis_detected else "medical-v1.0.0",
                )
                return self._with_stages(
                    self._guardrail_result(
                        risk,
                        user_id=user_id,
                        session_id=session_id,
                        sources=sources,
                        rewritten=True,
                    ),
                    stages,
                )
            if inspection.soft_failed:
                return self._with_stages(
                    self._fallback_result(
                        "AGENT_INSPECTION_FAILED",
                        request_id=getattr(self.main_client, "last_request_id", None),
                        sources=sources,
                        rewritten=True,
                        inspection=inspection,
                    ),
                    stages,
                )

        reply = candidate.reply or GENERIC_FALLBACK
        recommendation_id = None
        final_action = candidate.action
        final_error_code = candidate.errors[0] if candidate.errors else None
        if final_action is not None:
            start = time.perf_counter()
            try:
                explicit_request = resolve_explicit_intent(user_text) is not None
                recommendation = self.action_service.recommend(
                    user_id,
                    session_id,
                    final_action,
                    source_message_id=getattr(self.main_client, "last_request_id", None) or uuid.uuid4().hex,
                    deduplicate=not explicit_request,
                )
                recommendation_id = recommendation.recommendation_id
            except ActionAlreadyRecommended:
                final_action = None
            except ActionContractError:
                final_action = None
                final_error_code = "action_policy_violation"
            stages["action_ms"] = max(0, int((time.perf_counter() - start) * 1000))
        return self._with_stages(
            AgentResult(
                reply,
                candidate.intent,
                final_action,
                False,
                False,
                False,
                rewritten,
                inspection,
                sources,
                error_code=final_error_code,
                request_id=getattr(self.main_client, "last_request_id", None),
                main_model=self.settings.deepseek_model,
                prompt_version=self.settings.prompt_version,
                recommendation_id=recommendation_id,
            ),
            stages,
        )
