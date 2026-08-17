import asyncio

import httpx
from fastapi.testclient import TestClient

import api_server
from api_server import create_app
from tests.test_api_runtime_state import RecordingAgent
from tests.test_gateway_auth import ApiRuntimeState
from tests.test_production_config import production_settings
from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService


def hardened_app(monkeypatch, **settings_overrides):
    monkeypatch.setattr(api_server, "verify_schema", lambda _url: None)
    settings = production_settings(
        api_trusted_hosts="localhost,testserver,agent-api",
        **settings_overrides,
    )
    return create_app(
        RecordingAgent,
        settings=settings,
        runtime_state=ApiRuntimeState(),
        user_data_service=UserDataService(MemoryUserRepository()),
    )


def test_production_disables_docs_and_openapi(monkeypatch):
    with TestClient(hardened_app(monkeypatch)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_production_rejects_untrusted_host(monkeypatch):
    with TestClient(hardened_app(monkeypatch)) as client:
        response = client.get(
            "/health/live", headers={"Host": "attacker.example"}
        )

    assert response.status_code == 400


def test_request_size_boundary_rejects_only_above_configured_limit(monkeypatch):
    limit = 262_144
    with TestClient(hardened_app(monkeypatch, api_max_request_bytes=limit)) as client:
        at_limit = client.post(
            "/v1/chat",
            content=b"x" * limit,
            headers={"Content-Type": "application/json"},
        )
        over_limit = client.post(
            "/v1/chat",
            content=b"x" * (limit + 1),
            headers={"Content-Type": "application/json"},
        )

    assert at_limit.status_code != 413
    assert over_limit.status_code == 413
    assert over_limit.json()["error_code"] == "AGENT_REQUEST_TOO_LARGE"


def test_chunked_request_is_capped_without_content_length(monkeypatch):
    app = hardened_app(monkeypatch, api_max_request_bytes=262_144)

    async def chunks():
        for _ in range(257):
            yield b"x" * 1024

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                return await client.post(
                    "/v1/chat",
                    content=chunks(),
                    headers={"Content-Type": "application/json"},
                )

    response = asyncio.run(request())
    assert response.status_code == 413
    assert response.json()["error_code"] == "AGENT_REQUEST_TOO_LARGE"


def test_production_security_headers_no_store_and_no_cors(monkeypatch):
    with TestClient(hardened_app(monkeypatch)) as client:
        response = client.post(
            "/v1/chat",
            json={},
            headers={"Origin": "https://attacker.example"},
        )
        health = client.get("/health/live")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    assert health.json() == {"status": "alive"}
    assert "main_model" not in health.text
    assert "knowledge_chunks" not in health.text
