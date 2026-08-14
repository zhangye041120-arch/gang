# M7 Memory, Gateway Security, and Privacy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended; use superpowers:executing-plans when subagents are unavailable). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind user identity to trusted gateway requests, enforce server-side consent, provide durable long-term memory and action-event ingestion, and make privacy deletion complete.

**Architecture:** A gateway HMAC principal is verified before any user-scoped route and is replay-protected by the Redis runtime from Batch A. PostgreSQL is the source of truth for consent and versioned memories. `ActionService` remains the state-machine owner; a new internal API validates and records M1/M2/M3/M5 events, then creates minimal `action_summary` memories when authorized.

**Tech Stack:** Existing FastAPI/Pydantic/psycopg/memory/actions modules, Redis runtime from Batch A, PostgreSQL migrations, HMAC-SHA256.

## Global Constraints

- Do not change DeepSeek/Qwen chat models or prompt versions.
- User identity comes from the signed gateway principal, never from an arbitrary client body alone.
- Personalization and sensitive consent are server-side facts; request fields can only request less access.
- Store minimal action summaries; do not persist M5 post/comment text or unapproved sensitive source text.
- Every mutation is idempotent where an external retry can occur and every public response is schema-shaped.

---

### Task B1: Apply Schema v9 and Reconcile Consent Storage

**Files:**
- Create: `migrations/009_gateway_memory_privacy.sql`
- Modify: `xiaoliao_agent/migrations.py`
- Create: `tests/test_batch_b_migration.py`

**Interfaces:**
- Batch B sets `LATEST_SCHEMA_VERSION = 9`.
- `ai_consents` becomes the only production consent source.

- [ ] **Step 1: Write failing migration assertions**

Assert schema v9 adds memory provenance/version fields, action request fingerprints and summaries, `subject_hmac` on quality logs, privacy deletion audits/tombstones, user foreign keys with deletion behavior, and required delete grants. Seed conflicting legacy/new consent and assert the migration chooses the newer timestamp deterministically.

- [ ] **Step 2: Run migration tests and verify failure**

Run: `python -m pytest tests/test_batch_b_migration.py -q`

Expected: migration `009` and schema assertions do not exist.

- [ ] **Step 3: Implement migration v9**

Add `memory_key`, `supersedes_memory_id`, `source_type`, and `sensitive` to memories; `summary` and `request_fingerprint` to action events; `subject_hmac` to quality logs; and privacy deletion audit/tombstone tables. Reconcile missing/newer legacy `ai_memory_consents` rows into `ai_consents`, update production code first, then remove the legacy table to prevent future dual writes. Backfill safe defaults before creating partial unique indexes.

- [ ] **Step 4: Run empty, upgrade, replay, and cascade tests**

Run: `python -m pytest tests/test_batch_b_migration.py tests/test_migrations.py -q`

Expected: PASS.

### Task B2: Add Domain-Separated HMAC References

**Files:**
- Create: `xiaoliao_agent/content_refs.py`
- Modify: `xiaoliao_agent/user_data.py`
- Modify: `xiaoliao_agent/agent.py`
- Create: `tests/test_content_refs.py`
- Modify: `tests/test_agent_quality.py`, `tests/test_user_data.py`

**Interfaces:**
- `HmacReferenceService.subject_hmac(user_id)`, `conversation_ref(content)`, `candidate_reply_ref(content)`, and `fingerprint(purpose, canonical)` return versioned, domain-separated `hmac-sha256:<key-version>:<hex>` values.

- [ ] **Step 1: Write failing HMAC reference tests**

Assert identical input in different domains produces different values, key versions are explicit, old plain SHA-256 is no longer used for new conversation/quality rows, and no secret appears in representations or errors.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_content_refs.py tests/test_agent_quality.py tests/test_user_data.py -k 'hash or ref' -q`

Expected: missing service and old SHA-256 behavior failures.

- [ ] **Step 3: Implement and inject the service**

Use HMAC-SHA256 with distinct domain prefixes. Keep memory content dedup hashes unchanged because they are internal uniqueness keys; replace conversation/candidate/user references and supply `subject_hmac` to Redis/quality records.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_content_refs.py tests/test_agent_quality.py tests/test_user_data.py -q`

Expected: PASS.

### Task B3: Implement Gateway HMAC Principal Verification

**Files:**
- Create: `xiaoliao_agent/gateway_auth.py`
- Modify: `xiaoliao_agent/config.py`
- Modify: `api_server.py`
- Create: `tests/test_gateway_auth.py`

**Interfaces:**
- `canonical_gateway_message(timestamp, nonce, method, path, user_id, body_sha256) -> bytes`
- `sign_gateway_request(secret, timestamp, nonce, method, path, user_id, body_sha256) -> str`
- `GatewayAuthenticator.verify(request, body, expected_user_id) -> GatewayPrincipal`

- [ ] **Step 1: Write failing tests**

Cover valid signatures, missing/invalid headers, body tampering, user mismatch, clock skew, nonce replay, and constant-time comparison. Test development mode can use the existing service-token-only seam while production requires HMAC.

- [ ] **Step 2: Run `python -m pytest tests/test_gateway_auth.py -q` and verify failure**

Expected: import failure for the new authenticator.

- [ ] **Step 3: Implement canonical signing and verification**

Use `hmac.compare_digest`, strict ASCII header grammar, UTC epoch seconds, maximum skew from settings, and the Batch A Redis nonce guard. Never log the signature, secret, nonce, or raw body.

- [ ] **Step 4: Wire user-scoped routes**

Require and compare the signed subject for chat, summary, consent, memory CRUD, privacy deletion, and action events. Keep liveness public; keep readiness/admin routes service-scoped.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_gateway_auth.py tests/test_api_v1.py -k 'auth or user or privacy' -q`

Expected: PASS.

### Task B4: Enforce Server-Side Consent Truth

**Files:**
- Modify: `xiaoliao_agent/user_data.py`
- Modify: `xiaoliao_agent/agent.py`
- Modify: `api_server.py`
- Modify: `tests/test_user_data.py`
- Modify: `tests/test_api_v1.py`

**Interfaces:**
- Add `UserDataService.effective_consent(user_id, requested_personalization, requested_sensitive) -> tuple[bool, bool]`.

- [ ] **Step 1: Write failing consent tests**

Assert a request cannot enable personalization when the database consent is false, `user_summary` is omitted from model messages without consent, sensitive memory writes require both flags, and the same content produces a keyed digest rather than the old raw SHA-256.

- [ ] **Step 2: Run tests and verify old behavior fails**

Run: `python -m pytest tests/test_user_data.py tests/test_api_v1.py -k 'consent or summary or privacy' -q`

Expected: failures showing request-provided consent/summary still reaches the Agent and content refs use plain SHA-256.

- [ ] **Step 3: Implement server-side consent and hasher injection**

Pass the effective flags from `UserDataService` into `agent.chat`; keep compatibility for direct unit-test calls. Make `PostgresMemoryRepository` read/write `ai_consents` and remove production use of `ai_memory_consents`. Filter sensitive records on every read after consent revocation.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_user_data.py tests/test_api_v1.py tests/test_main_agent.py -k 'consent or summary or privacy or hash' -q`

Expected: PASS.

### Task B5: Add Versioned Memory Keys and CRUD

**Files:**
- Modify: `xiaoliao_agent/memory.py`
- Modify: `xiaoliao_agent/migrations.py` and `migrations/009_gateway_memory_privacy.sql`
- Modify: `xiaoliao_agent/api_contract.py`
- Modify: `api_server.py`
- Modify: `tests/test_memory.py`
- Create/modify: `tests/test_memory_api.py`

**Interfaces:**
- Extend `MemoryCandidate`/`MemoryRecord` with `memory_key`, `source_type`, `supersedes_memory_id`.
- Add `MemoryService.save_versioned_candidate()`, `list_current()`, `correct()`, `delete_one()`.
- Add `GET /v1/me/memories`, `PATCH /v1/me/memories/{memory_id}`, and `DELETE /v1/me/memories/{memory_id}`.

- [ ] **Step 1: Write failing tests**

Cover consent gating, one active record per `(user_id, memory_key)`, old-version invalidation, explicit correction provenance, cross-user access denial, pagination bounds, and immediate cache invalidation.

- [ ] **Step 2: Run memory tests to verify failure**

Run: `python -m pytest tests/test_memory.py tests/test_memory_api.py -q`

Expected: missing fields/methods/routes and cross-user behavior failures.

- [ ] **Step 3: Implement repository/service changes**

Use transactions for version replacement, preserve old rows as invalidated audit history, cap content at existing limits, and format only safe fields for API/model context. Keep lexical/recency retrieval when embeddings are not configured; do not add or replace a chat model.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_memory.py tests/test_memory_api.py -q`

Expected: PASS.

### Task B6: Add Idempotent M1/M2/M3/M5 Action Events

**Files:**
- Modify: `xiaoliao_agent/actions.py`
- Modify: `xiaoliao_agent/api_contract.py`
- Modify: `api_server.py`
- Modify: `tests/test_actions.py`
- Modify: `tests/test_actions_db.py`
- Create/modify: `tests/test_action_events_api.py`

**Interfaces:**
- Add strict `ActionEventRequest` with bounded `event_id`, `recommendation_id`, `user_id`, `module`, `event_type`, `occurred_at`, and allowlisted metadata.
- Add `POST /v1/action-events` returning `{status, recommendation_id, duplicate}`.
- Add repository `get_event(event_id)` and change `apply_event()` to return `(recommendation, created)` atomically. Persist a domain-separated request fingerprint so a changed replay returns `409`.

- [ ] **Step 1: Write failing tests**

Assert first completion returns `duplicate=false`, identical replay returns `duplicate=true` without a second memory, mismatched user/module is rejected, invalid transitions are rejected, metadata is bounded, and cross-user signatures fail.

- [ ] **Step 2: Run tests to verify old behavior fails**

Run: `python -m pytest tests/test_actions.py tests/test_actions_db.py tests/test_action_events_api.py -q`

Expected: the current helper reports the first event as duplicate and the PostgreSQL implementation has a multi-connection race.

- [ ] **Step 3: Implement atomic event handling and memory bridge**

Perform duplicate detection, recommendation validation, event insert, status update, and completion summary in one transaction for PostgreSQL. For memory mode retain the lock. Only create authorized minimal `action_summary` memories; do not turn declined events into negative labels.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_actions.py tests/test_actions_db.py tests/test_action_events_api.py -q`

Expected: PASS.

### Task B7: Implement Complete Privacy Deletion

**Files:**
- Create: `xiaoliao_agent/privacy.py`
- Modify: `xiaoliao_agent/user_data.py`
- Modify: `xiaoliao_agent/memory.py`, `xiaoliao_agent/actions.py`, `xiaoliao_agent/reminders.py`, `xiaoliao_agent/crisis.py`
- Modify: `api_server.py`
- Create/modify: `tests/test_privacy_deletion.py`
- Modify: `tests/test_api_v1.py`

**Interfaces:**
- `await PrivacyDeletionService.delete_user(user_id, subject_hmac, request_id) -> DeletionReport`
- `await PrivacyDeletionService.retry_pending(limit: int = 100) -> int`
- `DeletionReport` lists counts by resource, Redis cleanup status, and a keyed subject digest; it never includes the raw user ID in logs or response.

- [ ] **Step 1: Write failing deletion tests**

Seed memory, consent, conversation events, actions, reminders, crisis events, quality logs, and Redis keys; request deletion; assert all identifiable rows/keys disappear, the audit tombstone remains without raw user ID, and a failed Redis cleanup is visible/retryable. Race deletion against a memory/action write and assert tombstoned subjects cannot reappear.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_privacy_deletion.py tests/test_api_v1.py -k delete -q`

Expected: current deletion leaves action/crisis/reminder/quality state and deletes the existing audit row.

- [ ] **Step 3: Implement one-transaction database deletion**

Mark the subject as deleting before mutations, delete child tables in foreign-key order in one PostgreSQL transaction, remove crisis/quality rows under the approved default-delete policy, write an HMAC-only tombstone after deletion, and clear all user-scoped Redis state through the runtime registry. Redis cleanup failure records `redis_pending`; retries use only `subject_hmac`, never raw user ID.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_privacy_deletion.py tests/test_api_v1.py -k delete -q`

Expected: PASS.

### Task B8: Batch B Verification and Commit

- [ ] **Step 1:** Run `python -m pytest -q` and all gateway/memory/action/privacy integration tests with PostgreSQL + Redis.
- [ ] **Step 2:** Run a manual signed request/replay/cross-user matrix without printing secrets.
- [ ] **Step 3:** Run `git diff --check` and review SQL transaction boundaries.
- [ ] **Step 4:** Commit with `feat: 完成长效记忆与网关隐私控制`.
