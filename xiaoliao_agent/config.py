from dataclasses import dataclass
import os
from pathlib import Path
import re

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]

ACTION_WHITELIST = {
    "M1": {"page": "/pages/checkin/index", "allowed_params": frozenset()},
    "M2": {"page": "/pages/games/index", "allowed_params": frozenset()},
    "M3": {"page": "/pages/exercise/index", "allowed_params": frozenset()},
    "M5": {"page": "/pages/community/index", "allowed_params": frozenset()},
}
ACTION_WHITELIST_VERSION = "prototype-v1-pending-java-confirmation"
_DEFAULT_QUALITY_HASH_SALT = "xiaoliao-local-quality"


class ProductionConfigurationError(RuntimeError):
    def __init__(self, fields: list[str]):
        self.fields = tuple(sorted(set(fields)))
        super().__init__("生产配置不完整或不安全: " + ", ".join(self.fields))


def _load_env() -> None:
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / ".env")
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_api_key: str = ""
    qwen_model: str = "qwen3.7-flash-2026-07-15"
    temperature: float = 0.3
    max_tokens: int = 800
    inspector_max_tokens: int = 256
    inspector_enable_thinking: bool = False
    inspector_escalate_on_issues: bool = True
    live_lookup_enabled: bool = True
    live_lookup_timeout_seconds: float = 3.0
    live_default_city: str = "沈阳"
    web_search_enabled: bool = True
    web_search_provider: str = "dashscope"
    web_search_api_key: str = ""
    web_search_dashscope_model: str = "qwen3.7-flash-2026-07-15"
    web_search_forced_search: bool = False
    web_search_timeout_seconds: float = 45.0
    web_search_max_results: int = 5
    weather_provider: str = "wttr"
    weather_api_base: str = "https://wttr.in"
    weather_timeout_seconds: float = 6.0
    weather_cache_ttl_minutes: int = 10
    model_fallback_enabled: bool = True
    reminders_enabled: bool = True
    timeout_seconds: float = 60.0
    stream_timeout_seconds: float = 60.0
    rag_parallel_enabled: bool = True
    top_k: int = 3
    knowledge_version: str = "v1"
    embedding_provider: str = "none"
    embedding_model: str = ""
    embedding_dimension: int = 1536
    knowledge_database_url: str = ""
    rerank_enabled: bool = False
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_model: str = "qwen3-rerank"
    rerank_candidates: int = 12
    rerank_timeout_seconds: float = 30.0
    prompt_version: str = "1.7.0"
    action_decline_cooldown_hours: int = 24
    quality_hash_salt: str = _DEFAULT_QUALITY_HASH_SALT
    api_token: str = ""
    api_debug_token: str = ""
    api_admin_token: str = ""
    api_debug_enabled: bool = False
    api_port: int = 8081
    api_test_mode: bool = False
    api_rate_limit_per_minute: int = 60
    api_rate_limit_per_user: int = 20
    api_trusted_hosts: str = "localhost,127.0.0.1"
    api_max_request_bytes: int = 262_144
    api_workers: int = 2
    api_graceful_shutdown_seconds: int = 90
    api_keep_alive_seconds: int = 5
    api_forwarded_allow_ips: str = ""
    api_metrics_enabled: bool = True
    redis_url: str = ""
    redis_key_prefix: str = "xiaoliao"
    idempotency_ttl_seconds: int = 86_400
    idempotency_execution_ttl_seconds: int = 300
    nonce_ttl_seconds: int = 900
    gateway_hmac_secret: str = ""
    gateway_clock_skew_seconds: int = 300
    privacy_hmac_secret: str = ""
    privacy_hmac_key_version: str = "v1"
    backup_retention_days: int = 30
    crisis_route: str = "unconfigured"
    crisis_retention_days: int = 365
    crisis_notification_attempts: int = 2
    crisis_referral_config_path: str = ""
    crisis_notification_recipients: str = ""
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_agent_secret: str = ""
    wecom_callback_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_active_greeting_enabled: bool = False
    wecom_active_greeting_max_per_day: int = 1
    wecom_active_greeting_recent_active_hours: float = 24.0
    wecom_dedup_retention_hours: int = 168
    wecom_checkin_reminder_enabled: bool = False
    wecom_checkin_group_webhook: str = ""
    wecom_checkin_group_webhook_secret: str = ""
    wecom_checkin_group_times: str = "09:00"
    wecom_checkin_group_message: str = "小辽提醒：方便的时候花一分钟完成今天的情绪签到吧，记下此刻的心情就好，不用着急。"
    wecom_checkin_group_mention_all: bool = False
    wecom_checkin_group_mentioned_users: str = ""
    wecom_checkin_group_mentioned_mobiles: str = ""
    wecom_checkin_group_catch_up_minutes: int = 15
    wecom_checkin_group_state_path: str = ""
    wecom_checkin_group_user_schedule_path: str = ""
    memory_confidence_threshold: float = 0.8
    memory_max_items: int = 20
    memory_max_chars: int = 8000
    knowledge_path: Path = PROJECT_ROOT / "knowledge" / "CBT知识库_Agent版.md"
    lessons_path: Path = PROJECT_ROOT / "knowledge" / "lessons.md"
    lesson_search_top_k: int = 2
    memory_vector_search_enabled: bool = True
    memory_vector_min_score: float = 0.55
    aging_wordlist_enforced: bool = True
    tts_enabled: bool = False
    tts_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    tts_model: str = "qwen-audio-3.0-tts-flash"
    tts_voice: str = "longanhuan_v3.6"
    tts_format: str = "wav"
    tts_sample_rate: int = 24000

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env()
        return cls(
            app_env=os.getenv("APP_ENV", "development").strip().lower(),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", cls.deepseek_base_url),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", cls.deepseek_model),
            qwen_base_url=os.getenv("QWEN_BASE_URL", cls.qwen_base_url),
            qwen_api_key=os.getenv("QWEN_API_KEY", ""),
            qwen_model=os.getenv("QWEN_MODEL", cls.qwen_model),
            temperature=float(os.getenv("AGENT_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "800")),
            inspector_max_tokens=int(os.getenv("INSPECTOR_MAX_TOKENS", "256")),
            inspector_enable_thinking=os.getenv("INSPECTOR_ENABLE_THINKING", "false").strip().lower() == "true",
            inspector_escalate_on_issues=os.getenv("INSPECTOR_ESCALATE_ON_ISSUES", "true").strip().lower() == "true",
            live_lookup_enabled=os.getenv("LIVE_LOOKUP_ENABLED", "true").strip().lower() == "true",
            live_lookup_timeout_seconds=float(os.getenv("LIVE_LOOKUP_TIMEOUT_SECONDS", "3")),
            live_default_city=os.getenv("LIVE_DEFAULT_CITY", "沈阳"),
            web_search_enabled=os.getenv("WEB_SEARCH_ENABLED", "true").strip().lower() == "true",
            web_search_provider=os.getenv("WEB_SEARCH_PROVIDER", "dashscope"),
            web_search_api_key=os.getenv("WEB_SEARCH_API_KEY", ""),
            web_search_dashscope_model=os.getenv("WEB_SEARCH_DASHSCOPE_MODEL", "qwen3.7-flash-2026-07-15"),
            web_search_forced_search=os.getenv("WEB_SEARCH_FORCED_SEARCH", "false").strip().lower() == "true",
            web_search_timeout_seconds=float(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "45")),
            web_search_max_results=int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5")),
            weather_provider=os.getenv("WEATHER_PROVIDER", "wttr"),
            weather_api_base=os.getenv("WEATHER_API_BASE", "https://wttr.in"),
            weather_timeout_seconds=float(os.getenv("WEATHER_TIMEOUT_SECONDS", "6")),
            weather_cache_ttl_minutes=int(os.getenv("WEATHER_CACHE_TTL_MINUTES", "10")),
            model_fallback_enabled=os.getenv("MODEL_FALLBACK_ENABLED", "true").strip().lower() == "true",
            reminders_enabled=os.getenv("REMINDERS_ENABLED", "true").strip().lower() == "true",
            timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "60")),
            stream_timeout_seconds=float(os.getenv("AGENT_STREAM_TIMEOUT_SECONDS", "60")),
            rag_parallel_enabled=os.getenv("RAG_PARALLEL_ENABLED", "true").strip().lower() == "true",
            top_k=int(os.getenv("AGENT_TOP_K", "3")),
            knowledge_version=os.getenv("KNOWLEDGE_VERSION", "v1"),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "none"),
            embedding_model=os.getenv("EMBEDDING_MODEL", ""),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "1536")),
            knowledge_database_url=os.getenv("KNOWLEDGE_DATABASE_URL", ""),
            rerank_enabled=os.getenv("RERANK_ENABLED", "false").strip().lower() == "true",
            rerank_base_url=os.getenv("RERANK_BASE_URL", cls.rerank_base_url),
            rerank_model=os.getenv("RERANK_MODEL", cls.rerank_model),
            rerank_candidates=int(os.getenv("RERANK_CANDIDATES", "12")),
            rerank_timeout_seconds=float(os.getenv("RERANK_TIMEOUT_SECONDS", "30")),
            prompt_version=os.getenv("PROMPT_VERSION", "1.7.0"),
            action_decline_cooldown_hours=int(os.getenv("ACTION_DECLINE_COOLDOWN_HOURS", "24")),
            quality_hash_salt=os.getenv("QUALITY_HASH_SALT", _DEFAULT_QUALITY_HASH_SALT),
            api_token=os.getenv("API_TOKEN", ""),
            api_debug_token=os.getenv("API_DEBUG_TOKEN", ""),
            api_admin_token=os.getenv("API_ADMIN_TOKEN", ""),
            api_debug_enabled=os.getenv("API_DEBUG_ENABLED", "false").strip().lower() == "true",
            api_port=int(os.getenv("API_PORT", "8081")),
            api_test_mode=os.getenv("API_TEST_MODE", "false").strip().lower() == "true",
            api_rate_limit_per_minute=int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "60")),
            api_rate_limit_per_user=int(os.getenv("API_RATE_LIMIT_PER_USER", "20")),
            api_trusted_hosts=os.getenv("API_TRUSTED_HOSTS", "localhost,127.0.0.1"),
            api_max_request_bytes=int(os.getenv("API_MAX_REQUEST_BYTES", "262144")),
            api_workers=int(os.getenv("API_WORKERS", "2")),
            api_graceful_shutdown_seconds=int(os.getenv("API_GRACEFUL_SHUTDOWN_SECONDS", "90")),
            api_keep_alive_seconds=int(os.getenv("API_KEEP_ALIVE_SECONDS", "5")),
            api_forwarded_allow_ips=os.getenv("API_FORWARDED_ALLOW_IPS", ""),
            api_metrics_enabled=os.getenv("API_METRICS_ENABLED", "true").strip().lower() == "true",
            redis_url=os.getenv("REDIS_URL", ""),
            redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "xiaoliao"),
            idempotency_ttl_seconds=int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400")),
            idempotency_execution_ttl_seconds=int(os.getenv("IDEMPOTENCY_EXECUTION_TTL_SECONDS", "300")),
            nonce_ttl_seconds=int(os.getenv("NONCE_TTL_SECONDS", "900")),
            gateway_hmac_secret=os.getenv("GATEWAY_HMAC_SECRET", ""),
            gateway_clock_skew_seconds=int(os.getenv("GATEWAY_CLOCK_SKEW_SECONDS", "300")),
            privacy_hmac_secret=os.getenv("PRIVACY_HMAC_SECRET", ""),
            privacy_hmac_key_version=os.getenv("PRIVACY_HMAC_KEY_VERSION", "v1"),
            backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
            crisis_route=os.getenv("CRISIS_ROUTE", "unconfigured"),
            crisis_retention_days=int(os.getenv("CRISIS_RETENTION_DAYS", "365")),
            crisis_notification_attempts=int(os.getenv("CRISIS_NOTIFICATION_ATTEMPTS", "2")),
            crisis_referral_config_path=os.getenv("CRISIS_REFERRAL_CONFIG_PATH", ""),
            crisis_notification_recipients=os.getenv("CRISIS_NOTIFICATION_RECIPIENTS", ""),
            wecom_corp_id=os.getenv("WECOM_CORP_ID", ""),
            wecom_agent_id=os.getenv("WECOM_AGENT_ID", ""),
            wecom_agent_secret=os.getenv("WECOM_AGENT_SECRET", ""),
            wecom_callback_token=os.getenv("WECOM_CALLBACK_TOKEN", ""),
            wecom_encoding_aes_key=os.getenv("WECOM_ENCODING_AES_KEY", ""),
            wecom_active_greeting_enabled=os.getenv("WECOM_ACTIVE_GREETING_ENABLED", "false").strip().lower() == "true",
            wecom_active_greeting_max_per_day=int(os.getenv("WECOM_ACTIVE_GREETING_MAX_PER_DAY", "1")),
            wecom_active_greeting_recent_active_hours=float(os.getenv("WECOM_ACTIVE_GREETING_RECENT_ACTIVE_HOURS", "24")),
            wecom_dedup_retention_hours=int(os.getenv("WECOM_DEDUP_RETENTION_HOURS", "168")),
            wecom_checkin_reminder_enabled=os.getenv("WECOM_CHECKIN_REMINDER_ENABLED", "false").strip().lower() == "true",
            wecom_checkin_group_webhook=os.getenv("WECOM_CHECKIN_GROUP_WEBHOOK", ""),
            wecom_checkin_group_webhook_secret=os.getenv("WECOM_CHECKIN_GROUP_WEBHOOK_SECRET", ""),
            wecom_checkin_group_times=os.getenv("WECOM_CHECKIN_GROUP_TIMES", "09:00"),
            wecom_checkin_group_message=os.getenv("WECOM_CHECKIN_GROUP_MESSAGE", "小辽提醒：方便的时候花一分钟完成今天的情绪签到吧，记下此刻的心情就好，不用着急。"),
            wecom_checkin_group_mention_all=os.getenv("WECOM_CHECKIN_GROUP_MENTION_ALL", "false").strip().lower() == "true",
            wecom_checkin_group_mentioned_users=os.getenv("WECOM_CHECKIN_GROUP_MENTIONED_USERS", ""),
            wecom_checkin_group_mentioned_mobiles=os.getenv("WECOM_CHECKIN_GROUP_MENTIONED_MOBILES", ""),
            wecom_checkin_group_catch_up_minutes=int(os.getenv("WECOM_CHECKIN_GROUP_CATCH_UP_MINUTES", "15")),
            wecom_checkin_group_state_path=os.getenv("WECOM_CHECKIN_GROUP_STATE_PATH", ""),
            wecom_checkin_group_user_schedule_path=os.getenv("WECOM_CHECKIN_GROUP_USER_SCHEDULE_PATH", ""),
            memory_confidence_threshold=float(os.getenv("MEMORY_CONFIDENCE_THRESHOLD", "0.8")),
            memory_max_items=int(os.getenv("MEMORY_MAX_ITEMS", "20")),
            memory_max_chars=int(os.getenv("MEMORY_MAX_CHARS", "8000")),
            memory_vector_search_enabled=os.getenv("MEMORY_VECTOR_SEARCH_ENABLED", "true").strip().lower() == "true",
            memory_vector_min_score=float(os.getenv("MEMORY_VECTOR_MIN_SCORE", "0.55")),
            lesson_search_top_k=int(os.getenv("LESSON_SEARCH_TOP_K", "2")),
            aging_wordlist_enforced=os.getenv("AGING_WORDLIST_ENFORCED", "true").strip().lower() == "true",
            tts_enabled=os.getenv("TTS_ENABLED", "false").strip().lower() == "true",
            tts_base_url=os.getenv("TTS_BASE_URL", cls.tts_base_url),
            tts_model=os.getenv("TTS_MODEL", cls.tts_model),
            tts_voice=os.getenv("TTS_VOICE", cls.tts_voice),
            tts_format=os.getenv("TTS_FORMAT", cls.tts_format),
            tts_sample_rate=int(os.getenv("TTS_SAMPLE_RATE", "24000")),
        )

    def validate_live(self) -> None:
        missing = []
        if not self.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")
        if not self.qwen_api_key:
            missing.append("QWEN_API_KEY")
        if missing:
            raise RuntimeError(
                "缺少模型 API Key: " + ", ".join(missing) + "。请先运行“配置API.bat”。"
            )

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def trusted_hosts(self) -> tuple[str, ...]:
        return tuple(
            item.strip()
            for item in self.api_trusted_hosts.split(",")
            if item.strip()
        )

    def validate_production(self) -> None:
        if not self.is_production:
            return

        invalid: list[str] = []
        required_values = {
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
            "QWEN_API_KEY": self.qwen_api_key,
            "KNOWLEDGE_DATABASE_URL": self.knowledge_database_url,
            "REDIS_URL": self.redis_url,
            "CRISIS_NOTIFICATION_RECIPIENTS": self.crisis_notification_recipients,
        }
        invalid.extend(name for name, value in required_values.items() if not str(value).strip())

        strong_secrets = {
            "API_TOKEN": self.api_token,
            "GATEWAY_HMAC_SECRET": self.gateway_hmac_secret,
            "PRIVACY_HMAC_SECRET": self.privacy_hmac_secret,
            "QUALITY_HASH_SALT": self.quality_hash_salt,
        }
        for name, value in strong_secrets.items():
            if len(value) < 32:
                invalid.append(name)
        if self.quality_hash_salt == _DEFAULT_QUALITY_HASH_SALT:
            invalid.append("QUALITY_HASH_SALT")
        secret_values = list(strong_secrets.values())
        if len(set(secret_values)) != len(secret_values):
            invalid.append("PRODUCTION_SECRETS_MUST_DIFFER")

        if self.api_test_mode:
            invalid.append("API_TEST_MODE")
        if self.api_debug_enabled and len(self.api_debug_token) < 32:
            invalid.append("API_DEBUG_TOKEN")
        if self.crisis_route.strip().lower() == "unconfigured":
            invalid.append("CRISIS_ROUTE")
        if not self.trusted_hosts or "*" in self.trusted_hosts:
            invalid.append("API_TRUSTED_HOSTS")
        if not 131_072 <= self.api_max_request_bytes <= 2_097_152:
            invalid.append("API_MAX_REQUEST_BYTES")
        if not 30 <= self.gateway_clock_skew_seconds <= 900:
            invalid.append("GATEWAY_CLOCK_SKEW_SECONDS")
        if not 2 * self.gateway_clock_skew_seconds <= self.nonce_ttl_seconds <= 86_400:
            invalid.append("NONCE_TTL_SECONDS")
        if not 30 <= self.idempotency_execution_ttl_seconds <= 3_600:
            invalid.append("IDEMPOTENCY_EXECUTION_TTL_SECONDS")
        if not (
            2 * self.idempotency_execution_ttl_seconds
            <= self.idempotency_ttl_seconds
            <= 604_800
        ):
            invalid.append("IDEMPOTENCY_TTL_SECONDS")
        if not 1 <= self.backup_retention_days <= 365:
            invalid.append("BACKUP_RETENTION_DAYS")
        if not 1 <= self.api_workers <= 16:
            invalid.append("API_WORKERS")
        if not 10 <= self.api_graceful_shutdown_seconds <= 300:
            invalid.append("API_GRACEFUL_SHUTDOWN_SECONDS")
        if not 1 <= self.api_keep_alive_seconds <= 60:
            invalid.append("API_KEEP_ALIVE_SECONDS")
        if self.api_forwarded_allow_ips.strip() == "*":
            invalid.append("API_FORWARDED_ALLOW_IPS")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", self.redis_key_prefix):
            invalid.append("REDIS_KEY_PREFIX")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", self.privacy_hmac_key_version):
            invalid.append("PRIVACY_HMAC_KEY_VERSION")

        if invalid:
            raise ProductionConfigurationError(invalid)
