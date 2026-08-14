# M7 Delivery, Observability, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended; use superpowers:executing-plans when subagents are unavailable). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a repeatable non-root container, hardened FastAPI boundary, low-cardinality telemetry, CI checks, backup/restore tooling, and a release candidate gate.

**Architecture:** The API runs behind the trusted Java/WeCom network boundary with production-only FastAPI middleware and multiple workers. A dedicated metrics registry is injected per app instance so tests and workers do not collide. JSON logs and Prometheus metrics contain request IDs and low-cardinality dimensions only; no user text or memory content. Scheduled check-in delivery runs in a separate worker service so API workers cannot send duplicates.

**Tech Stack:** FastAPI/Starlette middleware, `prometheus-client`, Docker Compose, pinned Python requirements, GitHub Actions, PostgreSQL/Redis health checks.

## Global Constraints

- Do not expose Python API ports publicly in the production Compose file.
- Do not enable CORS, reload, debug traceback, or public interactive docs in production.
- Do not put secrets, `.env*`, raw user data, eval outputs, or logs in images/artifacts.
- Keep production model IDs unchanged.
- Every claimed release gate must have a fresh command result or external signed evidence.

---

### Task C1: Harden FastAPI Boundary

**Files:**
- Modify: `api_server.py`
- Modify: `xiaoliao_agent/config.py`
- Create: `tests/test_api_hardening.py`

**Interfaces:**
- Add production-only `TrustedHostMiddleware`, request-size middleware, security headers middleware, and configurable docs/openapi URLs.
- Add `/health/live` and `/health/ready` contracts from Batch A.
- Add `api_workers`, `api_graceful_shutdown_seconds`, `api_keep_alive_seconds`, and `api_forwarded_allow_ips` settings; reject wildcard forwarded proxies in production.

- [ ] **Step 1: Write failing tests**

Assert production docs/openapi are disabled, an untrusted Host is rejected, a 256 KiB body is accepted while one byte more returns `413` (including chunked input), security headers are present, CORS is absent, and health responses contain no model names or chunk counts.

- [ ] **Step 2: Run `python -m pytest tests/test_api_hardening.py -q` and verify failure**

Expected: middleware/config/route failures against the current default FastAPI app.

- [ ] **Step 3: Implement production-only middleware**

Read the body once and cap bytes without buffering unbounded data; preserve FastAPI validation. Configure trusted proxy IPs explicitly, set `Cache-Control: no-store` on sensitive responses, and keep development docs available for local debugging.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_api_hardening.py tests/test_api_v1.py -q`

Expected: PASS.

### Task C2: Add JSON Logging and Metrics

**Files:**
- Create: `xiaoliao_agent/observability.py`
- Modify: `api_server.py`
- Modify: `xiaoliao_agent/agent.py`
- Modify: `requirements.txt`
- Create: `tests/test_observability.py`

**Interfaces:**
- `configure_logging(environment: str) -> None`
- `Metrics(registry: CollectorRegistry)` with request/agent/dependency counters and histograms.
- `Metrics.snapshot()` for deterministic unit assertions.
- Add keyword-only `telemetry: Telemetry | None = None` and `readiness_service: ReadinessService | None = None` parameters to the existing `create_app` signature so registries and dependency probes remain injectable.

- [ ] **Step 1: Write failing tests**

Assert a request emits JSON fields `request_id`, route, status, latency, and error code; metrics use only allowlisted low-cardinality labels; two `create_app()` instances do not duplicate collectors; no user ID, body, token, or memory content appears.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_observability.py -q`

Expected: import/metric failures because no observability module exists.

- [ ] **Step 3: Implement instrumentation**

Inject a `CollectorRegistry` into `create_app`, instrument complete HTTP/SSE duration and Agent stage timings, expose an internal `/metrics` route only when enabled, and use stdlib JSON logging with redaction. Fixed metric names are `xiaoliao_http_requests_total`, `xiaoliao_http_request_duration_seconds`, `xiaoliao_agent_stage_duration_seconds`, `xiaoliao_model_errors_total`, `xiaoliao_idempotency_total`, `xiaoliao_dependency_unavailable_total`, `xiaoliao_memory_operations_total`, `xiaoliao_memory_cache_total`, and `xiaoliao_crisis_events_total`. Add close hooks for model HTTP pools and Redis.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_observability.py tests/test_api_v1.py -q`

Expected: PASS.

### Task C3: Build Repeatable Production Artifacts

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Modify: `docker-compose.yml`
- Create: `requirements.in`
- Create: `requirements-dev.in`
- Create: `requirements-dev.txt`
- Modify: `requirements.txt`
- Create: `pyproject.toml`
- Create: `tests/test_deployment_artifacts.py`

**Interfaces:**
- Compose services: `postgres`, `redis`, `migrate`, `agent-api`, and a single `checkin-worker`; only Postgres/Redis volumes are persistent.
- API image runs as non-root with a configurable worker count and no `--reload`.

- [ ] **Step 1: Write failing artifact tests**

Assert Dockerfile has a non-root `USER`, no reload, Compose includes healthchecks/dependency ordering and no public API port, `.dockerignore` excludes secrets/data, and runtime/dev requirements are separated and hash-pinned.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_deployment_artifacts.py -q`

Expected: missing artifact failures.

- [ ] **Step 3: Implement artifacts**

Use a digest-pinned Python slim base, a build stage for wheels, a UID/GID 10001 non-root runtime stage, explicit `uvicorn` command, Redis/Postgres healthchecks, a one-shot migration service, and environment-injected secrets. The API workers never start the check-in loop; one worker service owns scheduled delivery. Enable Redis AOF and container `read_only`, `/tmp` tmpfs, `cap_drop: ALL`, and `no-new-privileges`. Generate hash locks with `pip-tools` in a clean Linux Python 3.12 target environment, never from the current Conda environment. Do not bake model keys or database URLs into layers.

- [ ] **Step 4: Run artifact and build checks**

Run: `python -m pytest tests/test_deployment_artifacts.py -q`

Run: `docker compose config` and `docker build --check .` where Docker is available.

Expected: PASS; otherwise record the exact environment blocker without weakening checks.

### Task C4: Add CI, Security Scans, Backup and Restore

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/backup_database.py`
- Create: `scripts/restore_database.py`
- Create: `scripts/verify_release.py`
- Modify: `README.md`
- Modify: `docs/上线前置动作清单.md`
- Modify: `docs/上线回滚方案.md`
- Create: `tests/test_release_scripts.py`

**Interfaces:**
- CI runs unit tests, compile, offline eval/RAG gates, migration smoke with Postgres/Redis services, dependency audit, secret scan, and artifact build.
- Backup/restore scripts accept explicit destination/database parameters and refuse empty or unsafe paths.

- [ ] **Step 1: Write failing script/CI contract tests**

Assert workflow has no secret values, invokes all required checks, scripts use argument-list subprocess calls without a shell, backups are encrypted/permission-restricted by the deployment environment, restore requires an explicit different target database, and release verification refuses dirty/unversioned artifacts.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_release_scripts.py -q`

Expected: missing-file failures.

- [ ] **Step 3: Implement CI and scripts**

Use environment-provided credentials, never command-line secrets; write backups via a temporary file then atomically rename, constrain retention deletion to matching files under an approved directory, and document a disposable restore drill. Add `--gate`/`--check` exit-code modes to evaluation, RAG, and OpenAPI generation so CI cannot report green when assertions drift. Pin third-party GitHub Actions to immutable commit SHAs. Keep external 7-day, WeCom, crisis, and Java sign-offs as explicit manual gates.

- [ ] **Step 4: Run local release checks**

Run: `python -m pytest tests/test_release_scripts.py -q`, `python -m compileall -q .`, `python run_eval_suite.py --offline`, and `python run_rag_eval.py --offline`.

Expected: PASS with evidence files stored outside release secrets.

### Task C5: Freeze Contracts and Final Verification

- [ ] **Step 1:** Regenerate/verify `openapi.json`, API examples, and Java gateway HMAC example.
- [ ] **Step 2:** Run `python -m pytest -q --tb=short` after all merges.
- [ ] **Step 3:** Run container migration/startup/readiness smoke and a signed cross-user denial matrix.
- [ ] **Step 4:** Run dependency and secret scans; inspect image contents and user.
- [ ] **Step 5:** Update release docs only with observed evidence, never template completion claims.
- [ ] **Step 6:** Commit with `feat: 完成 M7 可部署上线路径`.
