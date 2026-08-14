# M7 Production Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production persistence, migrations, Redis-backed coordination, reminders, and readiness checks fail closed while preserving in-memory development/test behavior.

**Architecture:** `APP_ENV=production` selects PostgreSQL and Redis adapters and rejects unavailable dependencies. A standalone migration runner records ordered SQL checksums before the API starts. Redis provides shared rate limits, idempotency, replay protection, and bounded short-lived state; PostgreSQL remains the source of truth for users, memory, actions, reminders, and logs.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Redis 7 via `redis` async client, PostgreSQL/pgvector, pytest, Docker Compose.

## Global Constraints

- DeepSeek and Qwen chat model IDs and prompts remain unchanged.
- Production never falls back to an in-memory repository when PostgreSQL or Redis is unavailable.
- Development and test modes retain existing in-memory seams and do not require external services.
- Migration files are append-only; an applied migration checksum change is a hard failure.
- Redis stores only reconstructable coordination/cache state and every key has a TTL.

---

### Task A1: Add Environment-Aware Settings Validation

**Files:**
- Modify: `xiaoliao_agent/config.py`
- Create: `tests/test_production_config.py`

**Interfaces:**
- Add `app_env`, `redis_url`, `gateway_hmac_secret`, `privacy_hmac_secret`, `api_trusted_hosts`, `api_max_request_bytes`, `gateway_clock_skew_seconds`, `idempotency_ttl_seconds`, `idempotency_execution_ttl_seconds`, `nonce_ttl_seconds`, and `redis_key_prefix` to `Settings`.
- Add `Settings.is_production` and `Settings.validate_production()`; the latter raises `RuntimeError` with names only, never secret values.

- [ ] **Step 1: Write failing tests**

```python
def test_production_rejects_missing_durable_dependencies():
    settings = Settings(app_env="production", api_token="x" * 40)
    with pytest.raises(RuntimeError, match="KNOWLEDGE_DATABASE_URL"):
        settings.validate_production()

def test_development_allows_memory_mode():
    Settings(app_env="development").validate_production()

def test_production_rejects_test_mode_and_default_hash_secret():
    settings = Settings(
        app_env="production", api_test_mode=True,
        knowledge_database_url="postgresql://db", redis_url="redis://redis",
        api_token="x" * 40, gateway_hmac_secret="g" * 40,
        privacy_hmac_secret="p" * 40,
    )
    with pytest.raises(RuntimeError, match="API_TEST_MODE"):
        settings.validate_production()
```

- [ ] **Step 2: Run the tests and verify the expected failures**

Run: `python -m pytest tests/test_production_config.py -q`

Expected: import/attribute failures because the production fields and validator do not exist.

- [ ] **Step 3: Implement the minimal settings contract**

Parse environment variables with bounded numeric validation. Require PostgreSQL, Redis, API token length, gateway/privacy HMAC secrets, non-default quality salt, trusted hosts, and a non-`unconfigured` crisis route only when `app_env=production`. Keep `api_test_mode` forbidden in production.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_production_config.py -q`

Expected: PASS.

### Task A2: Implement Ordered Migration Runner and Production Schema

**Files:**
- Create: `xiaoliao_agent/migrations.py`
- Create: `migrate_database.py`
- Create: `migrations/008_agent_reminders.sql`
- Create: `tests/test_migrations.py`

**Interfaces:**
- `migration_files(root: Path) -> list[Path]`
- `plan_migrations(applied: dict[str, str], migrations: list[Migration]) -> list[Migration]`
- `latest_migration_version(root: Path) -> str`
- `apply_migrations(database_url: str, root: Path) -> list[str]`
- `verify_schema(database_url: str, expected_version: str, root: Path) -> None`

- [ ] **Step 1: Write failing unit tests**

```python
def test_migration_files_are_numeric_and_ordered(tmp_path):
    (tmp_path / "002_b.sql").write_text("select 1;", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("select 1;", encoding="utf-8")
    assert [p.name for p in migration_files(tmp_path)] == ["001_a.sql", "002_b.sql"]

def test_checksum_change_is_rejected(tmp_path):
    path = tmp_path / "001_first.sql"
    path.write_text("select 1;", encoding="utf-8")
    migration = migration_files(tmp_path)[0]
    with pytest.raises(MigrationChecksumMismatch):
        plan_migrations({migration.version: "wrong-checksum"}, [migration])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_migrations.py -q`

Expected: import/attribute failures for the migration runner.

- [ ] **Step 3: Add append-only migrations**

`008_agent_reminders.sql` creates durable reminders with a unique `(user_id, fingerprint)` index and indexes by due time. Keep all existing tables and constraints backward compatible; Batch B exclusively owns migration `009`.

- [ ] **Step 4: Implement the runner**

Create `schema_migrations(version, checksum, applied_at)`, acquire `pg_advisory_lock`, run each unapplied file inside a transaction, insert its SHA-256 checksum, and reject any checksum mismatch. Normalize CRLF/LF before hashing so Windows and Linux agree. The CLI accepts `--database-url`/environment and exits nonzero on failure without printing credentials.

- [ ] **Step 5: Run unit and optional PostgreSQL integration tests**

Run: `python -m pytest tests/test_migrations.py -q`

Run with a configured database: `python -m pytest tests/test_migrations.py -m integration -q`

Expected: unit tests pass; integration tests apply twice, preserve checksums, and reject tampering.

### Task A3: Make Reminders Durable

**Files:**
- Modify: `xiaoliao_agent/reminders.py`
- Modify: `xiaoliao_agent/agent.py`
- Modify: `tests/test_reminders.py`
- Modify: `tests/test_agent_pipeline.py`

**Interfaces:**
- Add `ReminderRepository` protocol-compatible methods `list_for_user()` and `add()`.
- Add `PostgresReminderRepository(database_url: str)`.
- Wire `XiaoliaoAgent` to choose PostgreSQL reminders in production/DB mode and memory reminders otherwise.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_agent_uses_postgres_reminders_when_database_is_configured():
    monkeypatch.setattr(agent_module, "_db_reachable", lambda *_args, **_kwargs: True)
    agent = XiaoliaoAgent(
        Settings(knowledge_database_url="postgresql://user:pass@db/xiaoliao"),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
    )
    assert isinstance(agent.reminder_service.repository, PostgresReminderRepository)
```

Add a repository round-trip integration test that creates the same fingerprint twice and asserts one row remains after a new service instance is created.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `python -m pytest tests/test_reminders.py tests/test_agent_pipeline.py -k reminder -q`

Expected: the production wiring test fails because the Agent always constructs `MemoryReminderRepository`.

- [ ] **Step 3: Implement the PostgreSQL adapter and wiring**

Use parameterized SQL, UTC-aware timestamps, and an atomic repository `create_if_absent()` guarded by the unique key/transaction so multiple workers cannot pass a check-then-insert race. Propagate the API request ID into reminder creation instead of reusing a model client's previous request ID. Do not store reminder request text beyond the bounded content field. Preserve existing in-memory tests.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_reminders.py tests/test_agent_pipeline.py -k reminder -q`

Expected: PASS.

### Task A4: Add Redis Runtime State Primitives

**Files:**
- Create: `xiaoliao_agent/runtime_state.py`
- Modify: `requirements.txt`
- Create: `tests/test_runtime_state.py`

**Interfaces:**
- `RedisRuntimeState.from_url(url: str, prefix: str) -> RedisRuntimeState`
- `await state.ping()`, `await state.aclose()`
- `await state.allow_rate(principal: str, user_id: str, per_minute: int, per_user: int) -> bool`
- `await state.consume_nonce(nonce: str, ttl_seconds: int) -> bool`
- `await state.claim_idempotency(scope: str, key: str, fingerprint: str) -> ClaimResult`; an execute claim includes a random execution token and bounded lease.
- `await state.finish_idempotency(scope: str, key: str, execution_token: str, status: int, body: dict) -> None`; a stale token cannot overwrite a newer execution.
- `await state.abandon_idempotency(scope: str, key: str, execution_token: str) -> None` releases a failed execution only when the token still owns the lease.
- `await state.register_subject_key(subject_hmac: str, key: str) -> None` and `await state.delete_subject(subject_hmac: str) -> int` support Batch B deletion without scanning or persisting raw user IDs in the registry.

- [ ] **Step 1: Write failing fake-Redis tests**

Test that the first claim returns `execute`, same fingerprint returns `cached` after finish, different fingerprint returns `conflict`, nonce replay is rejected, rate limits are shared by principal and user, and every key receives an expiry.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_runtime_state.py -q`

Expected: import failure for `RedisRuntimeState`.

- [ ] **Step 3: Implement atomic Redis operations**

Use `redis.asyncio`, Lua or transactional pipelines for the dual-bucket rate check and claim transition. Store only normalized JSON response bodies, cap their size, poll an executing claim until a bounded wait deadline, and return a typed unavailable result when Redis is down. Prefix all keys and set TTLs. Accept only an already-keyed `subject_hmac` for user state registration so the Redis layer never needs the privacy secret or raw user ID.

- [ ] **Step 4: Run tests and an optional real Redis smoke test**

Run: `python -m pytest tests/test_runtime_state.py -q`

Run with Redis: `python -m pytest tests/test_runtime_state.py -m integration -q`

Expected: PASS.

### Task A5: Wire Fail-Closed Startup and Readiness

**Files:**
- Modify: `xiaoliao_agent/agent.py`
- Modify: `api_server.py`
- Modify: `tests/test_api_v1.py`
- Modify: `tests/test_main_agent.py`

**Interfaces:**
- Add `GET /health/live` and `GET /health/ready`.
- `create_app()` initializes Redis runtime state in production, verifies schema and dependencies in lifespan, and closes state/model pools on shutdown. It accepts injectable `readiness_service` and `runtime_state` seams for tests.

- [ ] **Step 1: Write failing API tests**

```python
def test_live_health_is_minimal_and_public(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}

def test_ready_health_returns_503_when_redis_is_down(fake_agent):
    readiness = StubReadiness(redis_ok=False)
    app = create_app(fake_agent, api_token="test-token", test_mode=True,
                     readiness_service=readiness)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 503
```

Add a production startup test proving a configured-but-unreachable database never creates a memory Agent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api_v1.py -k 'health or production' -q`

Expected: route/behavior failures.

- [ ] **Step 3: Implement lifespan wiring and health contracts**

Keep test-mode factories injectable and ensure the default factory constructs `XiaoliaoAgent(config)` instead of re-reading the environment. Make readiness return only dependency names and generic statuses; never expose model names, counts, URLs, or exception details. Keep `/health` as a deprecated compatibility alias with the new minimal liveness shape.

- [ ] **Step 4: Run the full runtime-focused suite**

Run: `python -m pytest tests/test_production_config.py tests/test_migrations.py tests/test_reminders.py tests/test_runtime_state.py tests/test_api_v1.py -q`

Expected: PASS.

### Task A6: Batch A Verification and Commit

- [ ] **Step 1:** Run `python -m pytest -q` and `python -m compileall -q .`.
- [ ] **Step 2:** Run migration twice and Redis smoke tests in Docker.
- [ ] **Step 3:** Review `git diff --check`, ensure no secrets or `.env` files are staged.
- [ ] **Step 4:** Commit with `feat: 加固 M7 生产运行基础`.
