from fastapi.testclient import TestClient
import pytest

import API服务
from API服务 import create_app
from xiaoliao_agent.agent import XiaoliaoAgent
from xiaoliao_agent.config import Settings
from xiaoliao_agent.runtime import ReadinessResult


class MinimalAgent:
    def __init__(self):
        self.settings = Settings()
        self.kb = type("KB", (), {"chunks": ["one"]})()


class StubReadiness:
    def __init__(self, ready=True):
        self.ready = ready

    async def check(self):
        return ReadinessResult(
            ready=self.ready,
            checks={"postgres": self.ready, "redis": self.ready},
        )


def agent_factory():
    return MinimalAgent()


def test_liveness_and_legacy_health_are_minimal():
    app = create_app(agent_factory, api_token="test-token", test_mode=True)
    with TestClient(app) as client:
        live = client.get("/health/live")
        legacy = client.get("/health")

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert legacy.json() == {"status": "alive"}
    assert "main_model" not in legacy.text
    assert "knowledge_chunks" not in legacy.text


def test_readiness_returns_503_and_recovers_without_restart():
    readiness = StubReadiness(ready=False)
    app = create_app(
        agent_factory,
        api_token="test-token",
        test_mode=True,
        readiness_service=readiness,
    )
    with TestClient(app) as client:
        unavailable = client.get("/health/ready")
        readiness.ready = True
        recovered = client.get("/health/ready")

    assert unavailable.status_code == 503
    assert unavailable.json() == {"status": "not_ready"}
    assert recovered.status_code == 200
    assert recovered.json() == {"status": "ready"}


def test_production_agent_does_not_fallback_when_database_is_down(monkeypatch):
    from xiaoliao_agent import agent as agent_module

    monkeypatch.setattr(agent_module, "_db_reachable", lambda *_args, **_kwargs: False)
    settings = Settings(
        app_env="production",
        knowledge_database_url="postgresql://user:pass@db/xiaoliao",
    )

    with pytest.raises(RuntimeError, match="database"):
        XiaoliaoAgent(
            settings,
            main_client=object(),
            inspector_client=object(),
        )


def test_default_factory_uses_create_app_settings(monkeypatch):
    captured = []

    class CapturingAgent(MinimalAgent):
        def __init__(self, settings):
            super().__init__()
            captured.append(settings)

    monkeypatch.setattr(API服务, "XiaoliaoAgent", CapturingAgent)
    settings = Settings(
        app_env="test",
        deepseek_api_key="main",
        qwen_api_key="inspector",
        api_token="test-token",
    )
    app = create_app(settings=settings, test_mode=True)

    with TestClient(app):
        pass

    assert captured == [settings]
