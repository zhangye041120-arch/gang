import fakeredis.aioredis
from fastapi.testclient import TestClient

from API服务 import create_app
from xiaoliao_agent.api_contract import AgentResult, InspectionResult
from xiaoliao_agent.config import Settings
from xiaoliao_agent.runtime_state import RedisRuntimeState, RuntimeStateUnavailable


def payload():
    return {
        "user_id": "runtime_user",
        "session_id": "runtime_session",
        "message": "你好",
        "context": {"consent": {"personalization": False}, "user_summary": ""},
        "debug": False,
    }


def headers(idempotency=None):
    result = {"Authorization": "Bearer test-token"}
    if idempotency:
        result["Idempotency-Key"] = idempotency
    return result


class RecordingAgent:
    def __init__(self):
        self.calls = 0
        self.settings = Settings()
        self.kb = type("KB", (), {"chunks": ["one"]})()

    def chat(self, *args, **kwargs):
        self.calls += 1
        return AgentResult(
            reply="你好，我在。",
            intent="chat",
            action=None,
            blocked=False,
            crisis_detected=False,
            safety_violation=False,
            rewritten=False,
            inspection=InspectionResult(),
            sources=[],
        )


def runtime_state(server):
    client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return RedisRuntimeState(
        client,
        prefix="api-runtime",
        idempotency_ttl_seconds=600,
        execution_ttl_seconds=30,
        nonce_ttl_seconds=60,
    )


def test_redis_idempotency_is_shared_between_app_instances():
    server = fakeredis.FakeServer()
    agent = RecordingAgent()
    first_app = create_app(
        lambda: agent,
        api_token="test-token",
        test_mode=True,
        runtime_state=runtime_state(server),
    )
    second_app = create_app(
        lambda: agent,
        api_token="test-token",
        test_mode=True,
        runtime_state=runtime_state(server),
    )

    with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
        first = first_client.post(
            "/v1/chat", json=payload(), headers=headers("shared-idempotency")
        )
        second = second_client.post(
            "/v1/chat", json=payload(), headers=headers("shared-idempotency")
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert agent.calls == 1


class DownRuntimeState:
    async def ping(self):
        return True

    async def aclose(self):
        return None

    async def allow_rate(self, *args, **kwargs):
        raise RuntimeStateUnavailable("down")


def test_runtime_state_failure_returns_503_before_agent_call():
    agent = RecordingAgent()
    app = create_app(
        lambda: agent,
        api_token="test-token",
        test_mode=True,
        runtime_state=DownRuntimeState(),
    )

    with TestClient(app) as client:
        response = client.post("/v1/chat", json=payload(), headers=headers())

    assert response.status_code == 503
    assert response.json()["error_code"] == "AGENT_RUNTIME_STATE_UNAVAILABLE"
    assert agent.calls == 0
