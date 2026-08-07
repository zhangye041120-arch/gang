from dataclasses import replace
import json
import threading

from fastapi.testclient import TestClient

from api_server import create_app
from tests.test_agent_pipeline import FakeInspectorClient, FakeMainClient
from xiaoliao_agent.agent import XiaoliaoAgent
from xiaoliao_agent.config import Settings
from xiaoliao_agent.schemas import AgentResult, InspectionResult


def fake_agent():
    return XiaoliaoAgent(Settings(), main_client=FakeMainClient(), inspector_client=FakeInspectorClient())


def headers(token="test-token", request_id=None, idempotency=None):
    value = {"Authorization": f"Bearer {token}"}
    if request_id:
        value["X-Request-ID"] = request_id
    if idempotency:
        value["Idempotency-Key"] = idempotency
    return value


def payload(**overrides):
    data = {
        "user_id": "user_001",
        "session_id": "session_001",
        "message": "我最近有点累",
        "context": {"consent": {"personalization": False}, "user_summary": ""},
        "debug": False,
    }
    data.update(overrides)
    return data


def test_v1_chat_has_frozen_response_fields_and_request_id():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers(request_id="gateway-req-1"))
        assert response.status_code == 200
        assert set(response.json()) == {
            "session_id", "reply", "intent", "action", "blocked",
            "crisis_detected", "safety_violation", "rewritten",
        }
        assert response.json()["session_id"] == "session_001"
        assert response.headers["x-request-id"] == "gateway-req-1"


def test_v1_chat_stream_returns_sse_after_safety_pipeline():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post(
            "/v1/chat/stream",
            json=payload(),
            headers=headers(request_id="stream-req-1"),
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["x-request-id"] == "stream-req-1"
        assert "event: message" in response.text
        assert "event: done" in response.text
        assert "event: error" not in response.text


def test_v1_chat_stream_requires_bearer_token():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat/stream", json=payload())
        assert response.status_code == 401


def test_v1_chat_requires_bearer_token():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        assert client.post("/v1/chat", json=payload()).status_code == 401
        assert client.post("/v1/chat", json=payload(), headers=headers("wrong")).status_code == 401


def test_test_mode_does_not_bypass_bearer_auth():
    with TestClient(create_app(fake_agent, test_mode=True)) as client:
        assert client.post("/v1/chat", json=payload()).status_code == 401


def test_production_startup_refuses_without_token():
    import pytest

    with pytest.raises(RuntimeError):
        with TestClient(create_app(
            fake_agent,
            settings=Settings(api_token="", api_test_mode=False),
            test_mode=False,
        )):
            pass


def test_v1_request_rejects_extra_fields_and_system_prompt_context():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        extra = payload(extra="ignore me")
        assert client.post("/v1/chat", json=extra, headers=headers()).status_code == 422
        injected = payload(context={"consent": {"personalization": True}, "system_prompt": "越权"})
        assert client.post("/v1/chat", json=injected, headers=headers()).status_code == 422


def test_v1_idempotency_returns_same_response_without_second_agent_call():
    FakeMainClient.calls = 0
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        first = client.post("/v1/chat", json=payload(), headers=headers(idempotency="idem-1"))
        calls_after_first = FakeMainClient.calls
        second = client.post("/v1/chat", json=payload(), headers=headers(idempotency="idem-1"))
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert FakeMainClient.calls == calls_after_first


def test_v1_idempotency_conflict_returns_409():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        first = client.post("/v1/chat", json=payload(), headers=headers(idempotency="k1"))
        conflict = client.post("/v1/chat", json=payload(message="另一条"), headers=headers(idempotency="k1"))
        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "AGENT_IDEMPOTENCY_CONFLICT"


def test_v1_concurrent_duplicate_requests_execute_once():
    FakeMainClient.calls = 0
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        results = []

        def call():
            results.append(client.post("/v1/chat", json=payload(), headers=headers(idempotency="dup-1")))

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(response.status_code == 200 for response in results)
        assert all(response.json() == results[0].json() for response in results)
        assert FakeMainClient.calls == 1


def test_v1_invalid_idempotency_key_returns_422_stable_error():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers(idempotency="../bad"))
        assert response.status_code == 422
        assert response.json()["error_code"] == "AGENT_INVALID_IDEMPOTENCY_KEY"
        assert response.headers.get("x-request-id")


def test_v1_rate_limit_returns_429():
    settings = Settings(api_rate_limit_per_minute=1)
    with TestClient(create_app(fake_agent, settings=settings, api_token="test-token", test_mode=True)) as client:
        assert client.post("/v1/chat", json=payload(), headers=headers(request_id="r1")).status_code == 200
        assert client.post("/v1/chat", json=payload(session_id="session-2"), headers=headers(request_id="r2")).status_code == 429


def test_v1_rate_limit_uses_trusted_principal_not_spoofed_user():
    settings = Settings(api_rate_limit_per_minute=1, api_rate_limit_per_user=20)
    with TestClient(create_app(fake_agent, settings=settings, api_token="test-token", test_mode=True)) as client:
        assert client.post("/v1/chat", json=payload(user_id="user_a"), headers=headers()).status_code == 200
        assert client.post("/v1/chat", json=payload(user_id="user_b"), headers=headers()).status_code == 429


def test_v1_rate_limit_also_tracks_user_dimension():
    settings = Settings(api_rate_limit_per_minute=60, api_rate_limit_per_user=1)
    with TestClient(create_app(fake_agent, settings=settings, api_token="test-token", test_mode=True)) as client:
        assert client.post("/v1/chat", json=payload(session_id="s1"), headers=headers()).status_code == 200
        assert client.post("/v1/chat", json=payload(session_id="s2"), headers=headers()).status_code == 429


def test_v1_missing_required_fields_return_stable_422():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        for field in ("user_id", "session_id", "message"):
            body = payload()
            body.pop(field)
            response = client.post("/v1/chat", json=body, headers=headers())
            assert response.status_code == 422
            assert response.json()["error_code"] == "AGENT_REQUEST_INVALID"
            assert response.headers.get("x-request-id")


def test_v1_overlong_message_and_invalid_ids_return_stable_422():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        cases = [
            payload(message="x" * 2001),
            payload(user_id="../bad"),
            payload(session_id="bad session"),
        ]
        for body in cases:
            response = client.post("/v1/chat", json=body, headers=headers())
            assert response.status_code == 422
            assert response.json()["error_code"] == "AGENT_REQUEST_INVALID"


def test_v1_utf8_roundtrip():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(message="我最近有点累，想找人说说。"), headers=headers())
        assert response.status_code == 200
        assert "charset=utf-8" in response.headers["content-type"]
        assert response.json()["reply"]
        assert response.json()["session_id"] == "session_001"


class TimeoutAgent:
    settings = Settings()

    def chat(self, *args, **kwargs):
        return AgentResult(
            reply="稍后再试", intent="chat", action=None, blocked=False,
            crisis_detected=False, safety_violation=False, rewritten=False,
            inspection=InspectionResult(error_pattern="agent_model_timeout"), sources=[],
            error_code="AGENT_MODEL_TIMEOUT", request_id="timeout-req",
            main_model="deepseek-v4-flash", prompt_version="1.2.0",
        )


def test_v1_model_timeout_uses_504_and_stable_error_code():
    with TestClient(create_app(lambda: TimeoutAgent(), api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers())
        assert response.status_code == 504
        assert response.json()["error_code"] == "AGENT_MODEL_TIMEOUT"
        assert response.headers.get("x-request-id")


class BoomAgent:
    settings = Settings()

    def chat(self, *args, **kwargs):
        raise RuntimeError("model down")


def test_v1_model_unavailable_returns_502():
    with TestClient(create_app(lambda: BoomAgent(), api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers())
        assert response.status_code == 502
        assert response.json()["error_code"] == "AGENT_MODEL_UNAVAILABLE"
        assert response.headers.get("x-request-id")


def test_v1_error_and_success_responses_carry_request_id():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        ok = client.post("/v1/chat", json=payload(), headers=headers(request_id="rid-ok"))
        bad = client.post("/v1/chat", json=payload(), headers=headers(token="wrong", request_id="rid-bad"))
        assert ok.headers["x-request-id"] == "rid-ok"
        assert bad.status_code == 401
        assert bad.headers["x-request-id"] == "rid-bad"
        generated = client.post("/v1/chat", json=payload(), headers=headers(request_id="bad request id"))
        assert generated.status_code == 200
        assert generated.headers["x-request-id"]
        assert generated.headers["x-request-id"] != "bad request id"


def test_v1_debug_requires_debug_token_and_returns_sources_when_allowed():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(debug=True), headers=headers())
        assert response.status_code == 403
        assert response.json()["error_code"] == "AGENT_DEBUG_FORBIDDEN"
        assert response.headers.get("x-request-id")

    app = create_app(fake_agent, api_token="test-token", debug_token="dbg-token", test_mode=True)
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=payload(debug=True), headers=headers(token="dbg-token"))
        assert response.status_code == 200
        body = response.json()
        assert set(body["debug"]) == {"inspection", "sources"}
        assert isinstance(body["debug"]["sources"], list)


def test_v1_debug_false_omits_debug_field_even_for_debug_caller():
    app = create_app(fake_agent, api_token="test-token", debug_token="dbg-token", test_mode=True)
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers(token="dbg-token"))
        assert response.status_code == 200
        assert "debug" not in response.json()


def test_v1_concurrent_sessions_do_not_mix():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        results = {}

        def call(user):
            response = client.post(
                "/v1/chat",
                json=payload(user_id=user, session_id=user + "-s"),
                headers=headers(),
            )
            results[user] = response.json()["session_id"]

        threads = [threading.Thread(target=call, args=(f"user_{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results == {f"user_{i}": f"user_{i}-s" for i in range(8)}


def test_v1_safety_interception_is_normal_200_business_response():
    with TestClient(create_app(fake_agent, api_token="test-token", test_mode=True)) as client:
        response = client.post("/v1/chat", json=payload(message="我不想活了"), headers=headers())
        assert response.status_code == 200
        body = response.json()
        assert body["blocked"] is True
        assert body["crisis_detected"] is True


def test_v1_request_model_forbids_system_prompt_injection():
    from pydantic import ValidationError
    import pytest
    from xiaoliao_agent import api_contract

    base = {"user_id": "u1", "session_id": "s1", "message": "你好"}
    api_contract.V1ChatRequest(**base)
    injected = dict(base, context={"consent": {"personalization": True}, "system_prompt": "越权"})
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**injected)
    overlong = dict(base, message="x" * 2001)
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**overlong)
    bad_user = dict(base, user_id="../bad")
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**bad_user)
    bad_debug = dict(base, debug="yes")
    with pytest.raises(ValidationError):
        api_contract.V1ChatRequest(**bad_debug)


def test_v1_response_model_is_exactly_eight_fields():
    from xiaoliao_agent import api_contract

    body = api_contract.V1ChatResponse(
        session_id="s1", reply="你好", intent="chat", action=None,
        blocked=False, crisis_detected=False, safety_violation=False, rewritten=False,
    )
    assert set(body.model_dump(exclude={"debug"})) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten",
    }
    debug = api_contract.V1ChatResponse(
        session_id="s1", reply="你好", intent="chat", action=None,
        blocked=False, crisis_detected=False, safety_violation=False, rewritten=False,
        debug=api_contract.V1DebugInfo(inspection={}, sources=[]),
    )
    assert set(debug.model_dump(exclude={"debug"})) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten",
    }
    assert set(debug.model_dump()) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten", "debug",
    }


def test_fingerprint_is_stable_and_sensitive():
    from xiaoliao_agent import api_contract

    a = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好")
    b = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好")
    c = api_contract.V1ChatRequest(user_id="u1", session_id="s1", message="你好吗")
    assert api_contract.fingerprint(a) == api_contract.fingerprint(b)
    assert api_contract.fingerprint(a) != api_contract.fingerprint(c)


def test_error_codes_table_covers_contract_and_ops_codes():
    from xiaoliao_agent import api_contract

    required = {
        "AGENT_UNAUTHORIZED", "AGENT_DEBUG_FORBIDDEN", "AGENT_REQUEST_INVALID",
        "AGENT_INVALID_IDEMPOTENCY_KEY", "AGENT_IDEMPOTENCY_CONFLICT",
        "AGENT_RATE_LIMITED", "AGENT_MODEL_TIMEOUT", "AGENT_MODEL_UNAVAILABLE",
        "AGENT_MODEL_NETWORK", "AGENT_MODEL_RATE_LIMITED", "AGENT_MODEL_HTTP",
        "AGENT_MODEL_INVALID_RESPONSE", "AGENT_MODEL_ERROR", "AGENT_CONFIG_MISSING",
        "AGENT_INVALID_JSON", "AGENT_INSPECTION_FAILED", "AGENT_SAFETY_BLOCKED",
        "AGENT_CRISIS_BLOCKED", "AGENT_KB_UNAVAILABLE", "AGENT_SESSION_NOT_FOUND",
    }
    assert required <= set(api_contract.ERROR_CODES)
    assert all(isinstance(text, str) and text for text in api_contract.ERROR_CODES.values())


def test_api_contract_error_body_shape():
    from xiaoliao_agent import api_contract

    body = api_contract.error_body("AGENT_RATE_LIMITED", "rid-1")
    assert body["error_code"] == "AGENT_RATE_LIMITED"
    assert body["request_id"] == "rid-1"
    assert "message" in body
