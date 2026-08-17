import pytest

from xiaoliao_agent.config import ProductionConfigurationError, Settings


def production_settings(**overrides):
    values = {
        "app_env": "production",
        "deepseek_api_key": "deepseek-key",
        "qwen_api_key": "qwen-key",
        "knowledge_database_url": "postgresql://user:pass@postgres/xiaoliao",
        "redis_url": "redis://redis:6379/0",
        "api_token": "a" * 48,
        "gateway_hmac_secret": "g" * 48,
        "privacy_hmac_secret": "p" * 48,
        "quality_hash_salt": "q" * 48,
        "api_trusted_hosts": "agent-api,localhost,testserver",
        "crisis_route": "wecom-on-call",
        "crisis_notification_recipients": "on-call-user",
    }
    values.update(overrides)
    return Settings(**values)


def test_development_allows_memory_mode():
    Settings(app_env="development").validate_production()


@pytest.mark.parametrize("field,value,expected", [
    ("knowledge_database_url", "", "KNOWLEDGE_DATABASE_URL"),
    ("redis_url", "", "REDIS_URL"),
    ("api_token", "short", "API_TOKEN"),
    ("gateway_hmac_secret", "short", "GATEWAY_HMAC_SECRET"),
    ("privacy_hmac_secret", "short", "PRIVACY_HMAC_SECRET"),
    ("quality_hash_salt", "xiaoliao-local-quality", "QUALITY_HASH_SALT"),
    ("api_trusted_hosts", "*", "API_TRUSTED_HOSTS"),
    ("crisis_route", "unconfigured", "CRISIS_ROUTE"),
    ("crisis_notification_recipients", "", "CRISIS_NOTIFICATION_RECIPIENTS"),
])
def test_production_rejects_missing_or_unsafe_values(field, value, expected):
    settings = production_settings(**{field: value})

    with pytest.raises(ProductionConfigurationError) as exc_info:
        settings.validate_production()

    assert expected in exc_info.value.fields
    if value:
        assert str(value) not in str(exc_info.value)


def test_production_rejects_test_mode():
    settings = production_settings(api_test_mode=True)

    with pytest.raises(ProductionConfigurationError) as exc_info:
        settings.validate_production()

    assert "API_TEST_MODE" in exc_info.value.fields


def test_production_rejects_reused_secrets():
    settings = production_settings(
        gateway_hmac_secret="s" * 48,
        privacy_hmac_secret="s" * 48,
    )

    with pytest.raises(ProductionConfigurationError) as exc_info:
        settings.validate_production()

    assert "PRODUCTION_SECRETS_MUST_DIFFER" in exc_info.value.fields


@pytest.mark.parametrize("field,value,expected", [
    ("api_max_request_bytes", 1024, "API_MAX_REQUEST_BYTES"),
    ("gateway_clock_skew_seconds", 5, "GATEWAY_CLOCK_SKEW_SECONDS"),
    ("nonce_ttl_seconds", 100, "NONCE_TTL_SECONDS"),
    ("idempotency_execution_ttl_seconds", 0, "IDEMPOTENCY_EXECUTION_TTL_SECONDS"),
    ("idempotency_ttl_seconds", 60, "IDEMPOTENCY_TTL_SECONDS"),
    ("backup_retention_days", 0, "BACKUP_RETENTION_DAYS"),
    ("api_forwarded_allow_ips", "*", "API_FORWARDED_ALLOW_IPS"),
])
def test_production_rejects_unsafe_ranges(field, value, expected):
    settings = production_settings(**{field: value})

    with pytest.raises(ProductionConfigurationError) as exc_info:
        settings.validate_production()

    assert expected in exc_info.value.fields


def test_valid_production_settings_pass():
    production_settings().validate_production()


def test_from_env_reads_runtime_settings(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/7")
    monkeypatch.setenv("API_MAX_REQUEST_BYTES", "300000")
    monkeypatch.setenv("API_TRUSTED_HOSTS", "agent-api,localhost")
    monkeypatch.setenv("PRIVACY_HMAC_KEY_VERSION", "2026-08")
    monkeypatch.setenv(
        "PRIVACY_HMAC_PREVIOUS_KEYS",
        '{"2026-07":"old-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}',
    )

    settings = Settings.from_env()

    assert settings.app_env == "test"
    assert settings.redis_url == "redis://localhost:6379/7"
    assert settings.api_max_request_bytes == 300000
    assert settings.trusted_hosts == ("agent-api", "localhost")
    assert settings.privacy_hmac_key_version == "2026-08"
    assert settings.privacy_hmac_previous_keys == {
        "2026-07": "old-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    }


@pytest.mark.parametrize(
    "raw,sensitive_value",
    [
        ("not-json", "not-json"),
        ('[{"version":"v1","secret":"hidden-secret"}]', "hidden-secret"),
        ('{"v1":42}', '{"v1":42}'),
    ],
)
def test_from_env_rejects_invalid_previous_key_json_without_exposing_it(
    monkeypatch, raw, sensitive_value
):
    monkeypatch.setenv("PRIVACY_HMAC_PREVIOUS_KEYS", raw)

    with pytest.raises(ValueError) as exc_info:
        Settings.from_env()

    assert "PRIVACY_HMAC_PREVIOUS_KEYS" in str(exc_info.value)
    assert sensitive_value not in str(exc_info.value)


@pytest.mark.parametrize(
    "previous_keys",
    [
        {"bad version": "x" * 48},
        {"v0": "short"},
        {"v1": "x" * 48},
        {"v0": "p" * 48},
        {"v0": "x" * 48, "legacy": "x" * 48},
        {"v0": "g" * 48},
    ],
)
def test_production_rejects_unsafe_previous_hmac_keys(previous_keys):
    settings = production_settings(
        privacy_hmac_previous_keys=previous_keys,
    )

    with pytest.raises(ProductionConfigurationError) as exc_info:
        settings.validate_production()

    assert "PRIVACY_HMAC_PREVIOUS_KEYS" in exc_info.value.fields
    for secret in previous_keys.values():
        assert secret not in str(exc_info.value)


def test_settings_repr_does_not_expose_privacy_hmac_keyring():
    current_secret = "current-private-" + "x" * 32
    previous_secret = "previous-private-" + "y" * 32
    settings = Settings(
        privacy_hmac_secret=current_secret,
        privacy_hmac_previous_keys={"v0": previous_secret},
    )

    assert current_secret not in repr(settings)
    assert previous_secret not in repr(settings)


def test_valid_production_settings_accept_previous_hmac_keys():
    production_settings(
        privacy_hmac_previous_keys={
            "v0": "o" * 48,
            "legacy": "l" * 48,
        }
    ).validate_production()
