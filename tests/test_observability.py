import json
import logging

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from API服务 import create_app
from tests.test_api_runtime_state import RecordingAgent
from xiaoliao_agent.observability import Metrics, Telemetry, configure_logging


def headers(token="test-token", request_id="observable-request"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
    }


def payload(message="PRIVATE MESSAGE MUST NOT LOG"):
    return {
        "user_id": "private-user-id",
        "session_id": "private-session-id",
        "message": message,
        "context": {
            "consent": {"personalization": False},
            "user_summary": "PRIVATE MEMORY MUST NOT LOG",
        },
        "debug": False,
    }


def test_metrics_snapshot_uses_fixed_names_and_low_cardinality_labels():
    metrics = Metrics(CollectorRegistry())
    metrics.observe_http("POST", "/v1/chat", 200, 0.125)
    metrics.observe_agent_stage("main", 0.25)
    metrics.increment("idempotency", outcome="cached")

    snapshot = metrics.snapshot()
    assert snapshot["xiaoliao_http_requests_total"] == 1.0
    assert snapshot["xiaoliao_idempotency_total"] == 1.0
    assert "private-user" not in json.dumps(snapshot)


def test_two_app_instances_have_independent_metric_registries():
    first = create_app(RecordingAgent, api_token="test-token", test_mode=True)
    second = create_app(RecordingAgent, api_token="test-token", test_mode=True)

    assert first.state.telemetry.registry is not second.state.telemetry.registry


def test_request_log_and_metrics_never_include_user_body_token_or_memory(caplog):
    telemetry = Telemetry()
    app = create_app(
        RecordingAgent,
        api_token="super-secret-token",
        test_mode=True,
        telemetry=telemetry,
    )
    caplog.set_level(logging.INFO, logger="xiaoliao.http")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json=payload(),
            headers=headers("super-secret-token"),
        )

    assert response.status_code == 200
    record = next(
        json.loads(item.message)
        for item in caplog.records
        if item.name == "xiaoliao.http"
    )
    assert record["request_id"] == "observable-request"
    assert record["route"] == "/v1/chat"
    assert record["status"] == 200
    assert record["latency_ms"] >= 0
    rendered = "\n".join(item.message for item in caplog.records)
    for secret in (
        "private-user-id",
        "private-session-id",
        "PRIVATE MESSAGE MUST NOT LOG",
        "PRIVATE MEMORY MUST NOT LOG",
        "super-secret-token",
    ):
        assert secret not in rendered


def test_metrics_route_is_service_authenticated_and_contains_no_user_labels():
    app = create_app(RecordingAgent, api_token="test-token", test_mode=True)
    with TestClient(app) as client:
        client.post("/v1/chat", json=payload(), headers=headers())
        denied = client.get("/metrics")
        metrics = client.get("/metrics", headers=headers())

    assert denied.status_code == 401
    assert metrics.status_code == 200
    assert "xiaoliao_http_requests_total" in metrics.text
    assert "private-user-id" not in metrics.text


def test_configure_logging_emits_json_in_production(capsys):
    configure_logging("production")
    logging.getLogger("xiaoliao.test").info(
        json.dumps({"event": "test", "request_id": "r1"})
    )

    line = capsys.readouterr().err.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "xiaoliao.test"
    assert parsed["event"] == "test"
