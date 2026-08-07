# 小辽 M7 第 9 步 API 与 Java 合同实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 冻结并生产化 `POST /v1/chat` 外部合同：严格 Schema、Bearer 鉴权、限流、请求追踪、稳定错误码、并发隔离与单进程幂等，并交付 OpenAPI、错误码表、curl 与 Java 示例。

**Architecture:** 保持 FastAPI 单服务与 `create_app` 工厂；新增 `xiaoliao_agent/api_contract.py` 承载严格请求/响应模型、错误码表和幂等协调器；`api_server.py` 只做路由、鉴权和编排；真实 Agent 内核（`agent.chat`）不改业务规则，仅追加可选 `request_id` 透传。

**Tech Stack:** Python 3.10+、FastAPI、Pydantic v2、pytest、httpx TestClient、Java 11+ HttpClient（示例编译验证）。

## Global Constraints

- 外部 `intent` 固定为 `chat`、`checkin`、`game`、`exercise`、`assessment`、`community`；内部 `emotion_support` 映射为 `chat`。
- 生产鉴权为 `Authorization: Bearer <API_TOKEN>`；生产端口唯一配置源 `API_PORT`，默认 `8081`。
- `/v1/chat` 请求严格字段：`user_id`、`session_id`、`message`、`context`、`debug`；禁止额外字段。
- `/v1/chat` 响应严格字段：`session_id`、`reply`、`intent`、`action`、`blocked`、`crisis_detected`、`safety_violation`、`rewritten`。
- 安全/危机拦截是 HTTP 200 业务响应；模型超时 504、模型不可用 502、限流 429、鉴权 401，禁止用 200 隐藏系统故障。
- 幂等按“受信主体 + Idempotency-Key”隔离，单进程并发重复请求只执行一次 Agent。
- 当前目录不是 Git 仓库：计划中的 commit 步骤改为记录到 `M7_PROGRESS.md` 决策记录，不初始化仓库、不执行 git。
- 不修改主 Agent、Inspector、RAG、安全、记忆、行动的业务规则；唯一允许的签名变更是 `agent.chat(..., request_id="")` 透传。
- 真实 Java 联调未取得外部服务证据时，进度文件明确写“待外部资源”。

---

### Task 1: 合同模型与错误码模块

**Files:**
- Create: `xiaoliao_agent/api_contract.py`
- Test: `tests/test_api_v1.py`（新增用例）

**Interfaces:**
- Consumes: 无
- Produces:
  - `JAVA_INTENTS: tuple[str, ...]`（6 值）
  - `class V1Consent(BaseModel)`：`extra="forbid"`、strict、`personalization: StrictBool = False`
  - `class V1Context(BaseModel)`：`extra="forbid"`、strict、`consent: V1Consent`、`user_summary: StrictStr = ""`（max 2000）
  - `class V1ChatRequest(BaseModel)`：`extra="forbid"`、strict、`populate_by_name=False`；`user_id`（1-64，`^[A-Za-z0-9_:@.-]+$`）、`session_id`（1-128 同字符集）、`message`（1-2000）、`context: V1Context`、`debug: StrictBool = False`
  - `class V1ChatResponse(BaseModel)`：`extra="forbid"`；8 个固定字段
  - `class ApiContractError`：`__init__(self, status_code: int, error_code: str, message: str = "")`，属性同名
  - `ERROR_CODES: dict[str, str]`：错误码 -> 中文含义
  - `def fingerprint(payload: V1ChatRequest) -> str`：规范化 JSON 的 sha256 hex
  - `def error_body(error_code: str, request_id: str) -> dict[str, str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api_v1.py 追加
from xiaoliao_agent import api_contract

def test_v1_request_model_forbids_system_prompt_injection():
    from pydantic import ValidationError
    import pytest
    base = {"user_id": "u1", "session_id": "s1", "message": "你好"}
    api_contract.V1ChatRequest(**base)
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**base, context={"consent": {"personalization": True}, "system_prompt": "越权"})
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**base, message="x" * 2001)
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**base, user_id="../bad")

def test_v1_response_model_is_exactly_eight_fields():
    body = api_contract.V1ChatResponse(
        session_id="s1", reply="你好", intent="chat", action=None,
        blocked=False, crisis_detected=False, safety_violation=False, rewritten=False,
    )
    assert set(body.model_dump()) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten",
    }

def test_fingerprint_is_stable_and_sensitive():
    a = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好")
    b = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好")
    c = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好吗")
    assert api_contract.fingerprint(a) == api_contract.fingerprint(b)
    assert api_contract.fingerprint(a) != api_contract.fingerprint(c)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_api_v1.py -q`
Expected: FAIL（`ModuleNotFoundError: xiaoliao_agent.api_contract`）

- [ ] **Step 3: 实现 `xiaoliao_agent/api_contract.py`**

按 Interfaces 实现；`fingerprint` 使用 `payload.model_dump()` + `json.dumps(..., ensure_ascii=False, sort_keys=True)` + `sha256`。`ERROR_CODES` 至少包含：`AGENT_UNAUTHORIZED`、`AGENT_DEBUG_FORBIDDEN`、`AGENT_REQUEST_INVALID`、`AGENT_INVALID_IDEMPOTENCY_KEY`、`AGENT_IDEMPOTENCY_CONFLICT`、`AGENT_RATE_LIMITED`、`AGENT_MODEL_TIMEOUT`、`AGENT_MODEL_UNAVAILABLE`、`AGENT_MODEL_ERROR`、`AGENT_CONFIG_MISSING`、`AGENT_INVALID_JSON`、`AGENT_SAFETY_BLOCKED`、`AGENT_CRISIS_BLOCKED`、`AGENT_KB_UNAVAILABLE`、`AGENT_SESSION_NOT_FOUND`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_api_v1.py -q`
Expected: 新增 3 条 PASS，原有用例不回归。

- [ ] **Step 5: 记录**

在 `M7_PROGRESS.md` 决策记录追加一行：Task 1 完成，`api_contract` 模块与合同测试落地。

---

### Task 2: /v1/chat 安全与幂等编排

**Files:**
- Modify: `api_server.py`
- Modify: `xiaoliao_agent/agent.py`（仅 `chat` 增加 `request_id: str = ""` 参数并写入 `result.request_id`）
- Test: `tests/test_api_v1.py`（重写部分用例、新增用例）

**Interfaces:**
- Consumes: Task 1 全部符号
- Produces:
  - `create_app(agent_factory=None, *, settings=None, api_token=None, debug_token=None, test_mode=False)`
  - `authorize(request) -> (principal, is_debug)`：无效 Token 抛 `ApiContractError(401, "AGENT_UNAUTHORIZED")`
  - `class IdempotencyCoordinator`：
    - `claim(principal, key, fingerprint) -> ("execute"|"wait"|"cached", cached_entry_or_none)`
    - `finish(principal, key, status_code, body)`
  - `Settings` 新增字段：`api_debug_token: str = ""`（env `API_DEBUG_TOKEN`）

行为合同：
1. `request_id`：可信 `X-Request-ID` 需匹配 `[A-Za-z0-9_.:@-]{1,128}`，否则生成 uuid hex；所有响应头带 `X-Request-ID`。
2. 鉴权：Bearer Token 与 `config.api_token` 常量时间比较；`debug_token` 非空且匹配时主体拥有 debug 权限。
3. `debug=true` 且无 debug 权限 -> 403 `AGENT_DEBUG_FORBIDDEN`。
4. 限流：主体维度 + user_id 维度；超限 429 `AGENT_RATE_LIMITED`。
5. 幂等键非法 -> 422 `AGENT_INVALID_IDEMPOTENCY_KEY`；同键不同指纹 -> 409 `AGENT_IDEMPOTENCY_CONFLICT`；并发同键同指纹只有首个执行 Agent，其余等待并复用结果。
6. 现有 `test_v1_idempotency_returns_same_response_without_second_agent_call` 需同步修改：第二次请求使用与第一次完全相同的请求体（新合同下同键不同体为 409，由 `test_v1_idempotency_conflict_returns_409` 覆盖）。
6. `agent.chat` 抛异常 -> 502 `AGENT_MODEL_UNAVAILABLE`；`result.error_code == AGENT_MODEL_TIMEOUT` -> 504；其他 `AGENT_MODEL_*` -> 502。
7. 成功响应经 `V1ChatResponse` 序列化，仅 8 字段；`X-Request-ID` 头返回。
8. `/v1/chat` 不使用进程内 `ConversationStore`。

- [ ] **Step 1: 写失败测试**

```python
def test_v1_debug_requires_debug_token():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(debug=True), headers=headers())
        assert response.status_code == 403
        assert response.json()["error_code"] == "AGENT_DEBUG_FORBIDDEN"
    app = create_app(fake_agent, api_token="test-token", debug_token="dbg-token", test_mode=True)
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=payload(debug=True), headers=headers(token="dbg-token"))
        assert response.status_code == 200

def test_v1_idempotency_conflict_returns_409():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        first = client.post("/v1/chat", json=payload(), headers=headers(idempotency="k1"))
        conflict = client.post("/v1/chat", json=payload(message="另一条"), headers=headers(idempotency="k1"))
        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "AGENT_IDEMPOTENCY_CONFLICT"

def test_v1_concurrent_duplicate_requests_execute_once():
    import threading
    FakeMainClient.calls = 0
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        results = []
        def call():
            results.append(client.post("/v1/chat", json=payload(), headers=headers(idempotency="dup-1")))
        threads = [threading.Thread(target=call) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert all(r.status_code == 200 for r in results)
        assert all(r.json() == results[0].json() for r in results)
        assert FakeMainClient.calls == 1

def test_v1_model_unavailable_returns_502():
    class BoomAgent:
        settings = Settings()
        def chat(self, *args, **kwargs):
            raise RuntimeError("model down")
    with TestClient(create_app(lambda: BoomAgent(), api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers())
        assert response.status_code == 502
        assert response.json()["error_code"] == "AGENT_MODEL_UNAVAILABLE"

def test_v1_error_and_success_responses_carry_request_id():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        ok = client.post("/v1/chat", json=payload(), headers=headers(request_id="rid-ok"))
        bad = client.post("/v1/chat", json=payload(), headers=headers(token="wrong"))
        assert ok.headers["x-request-id"] == "rid-ok"
        assert bad.headers["x-request-id"]

def test_v1_concurrent_sessions_do_not_mix():
    import threading
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        results = {}
        def call(user):
            r = client.post("/v1/chat", json=payload(user_id=user, session_id=user + "-s"), headers=headers())
            results[user] = r.json()["session_id"]
        threads = [threading.Thread(target=call, args=(f"user_{i}",)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert results == {f"user_{i}": f"user_{i}-s" for i in range(8)}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_api_v1.py -q`
Expected: 新用例 FAIL（403/409/并发行为尚未实现）。

- [ ] **Step 3: 实现**

重写 `api_server.py` 中 `/v1/chat` 编排与 `authorize`；实现 `IdempotencyCoordinator`（`threading.Lock` + `threading.Event` 或 `Condition` 等待执行者完成）；`V1ChatRequest`/`V1ChatResponse` 改从 `xiaoliao_agent.api_contract` 导入；`agent.chat` 追加 `request_id` 关键字参数并在 `chat()` 起始处 `result.request_id = request_id or result.request_id`（`AgentResult.request_id` 已存在则只赋值，不改结构）；`Settings` 增加 `api_debug_token` 字段和 env 解析；`create_app` 增加 `debug_token` 参数；同时更新现有 `test_v1_idempotency_returns_same_response_without_second_agent_call` 使第二次请求复用相同请求体。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_api_v1.py tests/test_api.py -q`
Expected: PASS，旧合同测试（含 `/chat` 兼容路径）不回归。

- [ ] **Step 5: 记录**

`M7_PROGRESS.md` 追加 Task 2 决策行。

---

### Task 3: OpenAPI、错误码表与合同锁定测试

**Files:**
- Modify: `generate_openapi.py`
- Regenerate: `openapi.json`
- Create: `docs/API错误码表.md`
- Test: `tests/test_openapi_contract.py`

**Interfaces:**
- Consumes: Task 1/2 的 app 工厂
- Produces: `openapi.json`（含 `components.securitySchemes.bearerAuth`、`/v1/chat` 的 `security`、`V1ChatRequest`/`V1ChatResponse` schema、`intent` enum 6 值、弃用 `/chat`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_openapi_contract.py
import json
from api_server import create_app
from tests.test_api_v1 import fake_agent

def test_openapi_locks_bearer_security_and_v1_schemas():
    spec = create_app(fake_agent, api_token="t", test_mode=True).openapi()
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    path = spec["paths"]["/v1/chat"]["post"]
    assert {"bearerAuth": []} in path["security"]
    req_schema = spec["components"]["schemas"]["V1ChatRequest"]
    assert set(req_schema["required"]) == {"user_id", "session_id", "message"}
    assert req_schema.get("additionalProperties") is False or req_schema["properties"].keys() == {
        "user_id", "session_id", "message", "context", "debug"}
    resp_schema = spec["components"]["schemas"]["V1ChatResponse"]
    assert set(resp_schema["properties"]) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten"}
    assert resp_schema["properties"]["intent"]["enum"] == [
        "chat", "checkin", "game", "exercise", "assessment", "community"]
    assert spec["paths"]["/chat"]["post"]["deprecated"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_openapi_contract.py -q`
Expected: FAIL（尚无 securitySchemes）。

- [ ] **Step 3: 实现**

在 `api_server.py` 顶部定义 `bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")`（`from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials`），并给 `/v1/chat` 与 `/v1/knowledge/search` 增加依赖参数 `credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth)`；FastAPI 会自动生成 `components.securitySchemes.bearerAuth` 与逐操作 `security: [{"bearerAuth": []}]`，鉴权逻辑仍由 `authorize()` 手动执行（`auto_error=False` 保证缺头时由 401 分支处理）。给两个路由添加 `responses={401: ..., 422: ..., 429: ..., 502: ..., 504: ...}`，引用统一 `ApiError` schema（`error_code`、`request_id`、可选 `message`）。运行 `python generate_openapi.py` 重新生成 `openapi.json`；按 `api_contract.ERROR_CODES` 写 `docs/API错误码表.md`（错误码、HTTP、含义、Java 建议处理）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_openapi_contract.py -q`
Expected: PASS；`openapi.json` 已更新。

- [ ] **Step 5: 记录**

`M7_PROGRESS.md` 追加 Task 3 决策行。

---

### Task 4: Java 示例、curl、README、启动脚本与进度

**Files:**
- Create: `examples/java/V1ChatClient.java`
- Create: `docs/API联调示例.md`（curl + Java 说明）
- Modify: `README.md`、`.env.example`、`启动Agent接口.bat`、`M7_PROGRESS.md`
- Test: `tests/test_java_example.py`

**Interfaces:**
- Consumes: 冻结后的 `/v1/chat` 合同、`run_api.py`
- Produces: 可 `javac` 编译的 Java 11+ 示例；README 合同章节；`.env.example` 增加 `API_DEBUG_TOKEN=`；启动脚本端口唯一来自 `API_PORT`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_java_example.py
import shutil
import subprocess
from pathlib import Path

def test_java_example_compiles():
    if shutil.which("javac") is None:
        import pytest
        pytest.skip("本机无 javac，Java 编译验证待外部资源")
    root = Path(__file__).resolve().parents[1]
    out = root / "examples" / "java" / "build"
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["javac", "-encoding", "UTF-8", "-d", str(out), str(root / "examples/java/V1ChatClient.java")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_java_example.py -q`
Expected: FAIL（文件不存在）。

- [ ] **Step 3: 实现**

`V1ChatClient.java`：`java.net.http.HttpClient` 调用 `/v1/chat`，Bearer Token 从参数/环境变量读取，打印响应与 `X-Request-ID`，演示 `Idempotency-Key`；`docs/API联调示例.md` 给出 curl（成功、超时重试、幂等）与 Java 运行命令；README 新增“Java 合同（v1）”章节：端口唯一来源 `API_PORT`、鉴权、字段、错误码表链接、`/chat` 弃用说明；`.env.example` 增加 `API_DEBUG_TOKEN=`；`启动Agent接口.bat` 去掉硬编码 8081 文案，改为读取 `.env` 的 `API_PORT`（默认 8081）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_java_example.py -q`
Expected: PASS（本机有 javac 则编译通过）。

- [ ] **Step 5: 尝试本地 Java 真实调用（证据收集）**

启动 `python run_api.py`（使用 `.env` 真实配置或 `API_TEST_MODE=true`），运行 `java V1ChatClient http://127.0.0.1:<port>`；若真实模型 Key 可用且返回 200，将证据（脱敏）写入 `M7_PROGRESS.md`；失败或无法启动则写“待外部资源”。

- [ ] **Step 6: 记录**

`M7_PROGRESS.md` 更新：步骤 09 状态、测试数量、阻塞项（真实 Java/企微联调）、决策记录。

---

### Task 5: 全量回归与收尾

- [ ] **Step 1: 全量测试**

Run: `python -m pytest -q`
Expected: 全部 PASS（数量 >= 131 + 新增）。

- [ ] **Step 2: 生成物复核**

Run: `python generate_openapi.py`
Expected: 无报错；`openapi.json` 与测试锁定一致。

- [ ] **Step 3: 进度终版**

更新 `M7_PROGRESS.md`：当前阶段指向第 10 步、最新测试数与日期、外部资源栏记录 Java 联调状态（完成或待外部资源）。
