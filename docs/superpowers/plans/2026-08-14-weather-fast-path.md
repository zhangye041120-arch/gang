# Weather Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return complete, sourced weather answers quickly and make short follow-ups such as “具体点” continue the prior weather topic without model hallucination.

**Architecture:** Resolve direct weather questions and bounded short follow-ups before the general RAG/model pipeline. When the live provider returns `live:weather`, construct an `AgentResult` deterministically and skip the knowledge base, main model, inspector, and rewrite stages; otherwise reuse the fetched live fallback in the existing pipeline.

**Tech Stack:** Python 3, `httpx`, `pytest`, existing `XiaoliaoAgent`, `LiveContext`, and `AgentResult` contracts.

## Global Constraints

- Preserve the existing deterministic `precheck` before any weather lookup.
- A complete current-day answer includes condition, current temperature, feels-like temperature, high/low, humidity, wind, and rain probability or a rain reminder.
- Never infer UV, air quality, or other fields absent from the weather response.
- A weather fast-path response has `intent="chat"`, `action=None`, and retains `live:weather` sources.
- Do not overwrite the user's existing uncommitted changes in `.env.example`, `tests/test_live_context.py`, `xiaoliao_agent/config.py`, or `xiaoliao_agent/live_context.py`.

---

### Task 1: Complete Deterministic Weather Content

**Files:**
- Modify: `tests/test_live_context.py`
- Modify: `xiaoliao_agent/live_context.py`

**Interfaces:**
- Consumes: `_max_rain_chance(day: dict[str, Any]) -> int`
- Produces: `_fetch_weather(...) -> LiveContext | None` whose current-day `content` always states the daytime maximum rain probability.

- [ ] **Step 1: Write the failing test**

Extend the existing mocked current-weather test to assert all required fields and the low-rain case:

```python
assert "气温26℃" in result.content
assert "体感28℃" in result.content
assert "湿度70%" in result.content
assert "风力3级" in result.content
assert "22~30℃" in result.content
assert "降雨概率10%" in result.content
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_live_context.py::test_weather_routes_to_realtime_weather_api -q`

Expected: FAIL because low rain probability is currently omitted.

- [ ] **Step 3: Implement the minimal template change**

Compute `rain_chance = _max_rain_chance(day)` once. Always append `白天最高降雨概率{rain_chance}%。`; append the existing umbrella reminder only when the value is at least 40.

- [ ] **Step 4: Run the focused weather tests**

Run: `pytest tests/test_live_context.py -q`

Expected: PASS.

### Task 2: Resolve Bounded Weather Follow-ups

**Files:**
- Modify: `tests/test_live_context.py`
- Modify: `xiaoliao_agent/live_context.py`

**Interfaces:**
- Produces: `resolve_weather_query(text: str, history: list[dict[str, str]] | None = None) -> str | None`
- Behavior: return direct weather text unchanged; return the previous weather user query for `具体点`/`详细点`; synthesize the prior city plus the requested day for `那明天呢`; otherwise return `None`.

- [ ] **Step 1: Write parameterized failing tests**

```python
@pytest.mark.parametrize("follow_up,expected", [
    ("具体点", "沈阳今天天气怎么样？"),
    ("详细点", "沈阳今天天气怎么样？"),
    ("那明天呢", "沈阳明天天气"),
])
def test_resolve_weather_follow_up(follow_up, expected):
    history = [
        {"role": "user", "content": "沈阳今天天气怎么样？"},
        {"role": "assistant", "content": "沈阳今天晴，30度。"},
    ]
    assert resolve_weather_query(follow_up, history) == expected

def test_resolve_weather_follow_up_requires_weather_history():
    history = [
        {"role": "user", "content": "我最近睡不好"},
        {"role": "assistant", "content": "您想具体说说吗？"},
    ]
    assert resolve_weather_query("具体点", history) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_live_context.py -q`

Expected: collection/import failure because `resolve_weather_query` does not exist.

- [ ] **Step 3: Implement the pure resolver**

Use anchored regular expressions for the finite follow-up vocabulary. Inspect only the immediately preceding user/assistant turn, require `WEATHER_KEYWORDS` in the previous user message or assistant response, and use `extract_city` for day-changing follow-ups. Do not retain global conversation state.

- [ ] **Step 4: Run the resolver and weather tests**

Run: `pytest tests/test_live_context.py -q`

Expected: PASS.

### Task 3: Add Sync and Stream Weather Fast Paths

**Files:**
- Modify: `tests/test_agent_pipeline.py`
- Modify: `xiaoliao_agent/agent.py`

**Interfaces:**
- Consumes: `resolve_weather_query(...)`, `XiaoliaoAgent._fetch_live(...)`, `LiveContext.label`, and `LiveContext.sources`.
- Produces: `XiaoliaoAgent._weather_result(live: LiveContext) -> AgentResult` and fast-path handling in both `_chat_impl` and `chat_stream`.
- Extends: `_fetch_rag(..., live_query: str = "", preloaded_live: LiveContext | None = None)` so a failed realtime lookup can reuse its web-search fallback without a second network request.

- [ ] **Step 1: Write failing synchronous pipeline tests**

Create recording clients whose `chat` raises if called, supply a `live:weather` provider, and assert:

```python
result = agent.chat("沈阳今天天气怎么样？")
assert result.reply == live.content
assert result.intent == "chat"
assert result.action is None
assert result.sources == live.sources
assert main.calls == 0
assert inspector.calls == 0
```

Add a second call with weather history and `具体点`; assert the provider receives the previous weather query and both clients remain unused. Add a non-weather-history test proving `具体点` still calls the normal model pipeline.

- [ ] **Step 2: Run synchronous tests to verify they fail**

Run: `pytest tests/test_agent_pipeline.py -k "weather_fast or weather_follow" -q`

Expected: FAIL because the model pipeline is still invoked.

- [ ] **Step 3: Implement the synchronous fast path and fallback reuse**

After `precheck`, call `resolve_weather_query`. Fetch once when it resolves. If `live.label == "live:weather"`, return `_weather_result`; otherwise pass `live_query` and `preloaded_live` into `_fetch_rag`. Preserve `precheck_ms`, add `weather_ms`, and let `chat()` add `total_ms`.

- [ ] **Step 4: Run synchronous tests**

Run: `pytest tests/test_agent_pipeline.py -k "weather_fast or weather_follow or fetch_rag" -q`

Expected: PASS.

- [ ] **Step 5: Write and verify a failing stream test**

```python
events = list(agent.chat_stream("沈阳今天天气怎么样？"))
reply = "".join(e["content"] for e in events if e.get("type") == "token")
done = next(e for e in events if e.get("type") == "done")
assert reply == live.content
assert done["intent"] == "chat"
assert main.calls == 0
assert inspector.calls == 0
```

Run: `pytest tests/test_agent_pipeline.py -k "stream_weather_fast" -q`

Expected: FAIL because `chat_stream` still invokes the model pipeline.

- [ ] **Step 6: Implement and verify the stream fast path**

Reuse `_weather_result`, emit the deterministic reply in existing four-character token chunks, then emit one `done` event with `blocked=False`, `rewritten=False`, `intent="chat"`, `action=None`, and measured latency.

Run: `pytest tests/test_agent_pipeline.py -k "weather_fast or weather_follow" -q`

Expected: PASS.

### Task 4: Make Invalid-JSON Fallback Topic-Neutral

**Files:**
- Modify: `tests/test_main_agent.py`
- Modify: `xiaoliao_agent/agent.py`
- Modify: `xiaoliao_agent/guardrails.py`

**Interfaces:**
- Produces the exact fallback: `我刚才没能把这句话接稳。请再说一次，我会接着刚才的话回答。`

- [ ] **Step 1: Write the failing assertion**

Update `test_plain_text_main_output_uses_safe_fallback_without_inspection` to assert the exact topic-neutral copy and assert `最困扰` is absent.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_main_agent.py::test_plain_text_main_output_uses_safe_fallback_without_inspection -q`

Expected: FAIL with the old emotion-specific fallback.

- [ ] **Step 3: Update the fallback constants**

Change `AGENT_MODEL_INVALID_RESPONSE`, `AGENT_INVALID_JSON`, and `GENERIC_FALLBACK` to the approved neutral copy. Leave timeout, network, HTTP, rate-limit, and inspection-failure text unchanged.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_main_agent.py tests/test_guardrails.py tests/test_inspector.py -q`

Expected: PASS.

### Task 5: Regression and Real-World Verification

**Files:**
- Verify only; no planned production edits.

**Interfaces:**
- Verifies the end-to-end public `chat` behavior and latency evidence.

- [ ] **Step 1: Run all focused suites**

Run: `pytest tests/test_live_context.py tests/test_agent_pipeline.py tests/test_main_agent.py tests/test_api.py tests/test_api_v1.py -q`

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run: `pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Re-run the real two-turn conversation**

Call `agent.chat("沈阳今天天气怎么样？", [])`, append the result to history, then call `agent.chat("具体点", history)`. Print replies, sources, errors, and `stage_latencies`.

Expected: both replies use `live:weather`; both contain complete sourced data; neither contains unsupported UV data or the generic fallback; the second call uses the weather cache and neither result contains `main_ms` or `inspector_ms`.

- [ ] **Step 4: Review the final diff**

Run: `git diff --check` and `git diff --stat`.

Expected: no whitespace errors and only scoped implementation/test changes plus the implementation plan.
