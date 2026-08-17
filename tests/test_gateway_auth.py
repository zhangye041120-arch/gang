import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
from api_server import create_app
from tests.test_api_runtime_state import RecordingAgent
from tests.test_production_config import production_settings
from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService
from xiaoliao_agent.gateway_auth import (
    GatewayAuthenticationError,
    GatewayAuthenticator,
    canonical_gateway_message,
    sign_gateway_request,
)


SECRET = "gateway-test-secret-that-is-at-least-32-bytes"


class NonceState:
    def __init__(self):
        self.seen = set()
        self.calls = []

    async def consume_nonce(self, nonce, ttl_seconds=None):
        self.calls.append((nonce, ttl_seconds))
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


class Request:
    def __init__(self, headers, *, method="POST", path="/v1/chat"):
        self.headers = headers
        self.method = method
        self.url = SimpleNamespace(path=path)


def signed_headers(body, *, user_id="user-1", nonce="nonce-1234567890", timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    body_sha256 = hashlib.sha256(body).hexdigest()
    signature = sign_gateway_request(
        SECRET,
        timestamp,
        nonce,
        "POST",
        "/v1/chat",
        user_id,
        body_sha256,
    )
    return {
        "X-Gateway-Timestamp": str(timestamp),
        "X-Gateway-Nonce": nonce,
        "X-Gateway-User-ID": user_id,
        "X-Gateway-Signature": signature,
    }


def test_canonical_message_is_unambiguous_and_signing_is_deterministic():
    message = canonical_gateway_message(
        1_786_659_200,
        "nonce-1234567890",
        "post",
        "/v1/chat",
        "user-1",
        "a" * 64,
    )

    assert message == (
        b"1786659200\nnonce-1234567890\nPOST\n/v1/chat\nuser-1\n" + b"a" * 64
    )
    assert sign_gateway_request(
        SECRET, 1_786_659_200, "nonce-1234567890", "post", "/v1/chat", "user-1", "a" * 64
    ) == sign_gateway_request(
        SECRET, 1_786_659_200, "nonce-1234567890", "POST", "/v1/chat", "user-1", "a" * 64
    )


def test_valid_signature_returns_trusted_principal_and_consumes_nonce_once():
    body = b'{"user_id":"user-1","message":"hello"}'
    state = NonceState()
    authenticator = GatewayAuthenticator(SECRET, state, clock_skew_seconds=300)

    principal = asyncio.run(
        authenticator.verify(Request(signed_headers(body)), body, "user-1")
    )

    assert principal.user_id == "user-1"
    assert principal.subject_hmac.startswith("hmac-sha256:")
    assert state.calls == [("nonce-1234567890", 600)]


@pytest.mark.parametrize(
    "header",
    [
        "X-Gateway-Timestamp",
        "X-Gateway-Nonce",
        "X-Gateway-User-ID",
        "X-Gateway-Signature",
    ],
)
def test_missing_gateway_headers_are_rejected(header):
    body = b"{}"
    headers = signed_headers(body)
    del headers[header]

    with pytest.raises(GatewayAuthenticationError):
        asyncio.run(
            GatewayAuthenticator(SECRET, NonceState()).verify(
                Request(headers), body, "user-1"
            )
        )


def test_body_tampering_and_user_mismatch_are_rejected_without_consuming_nonce():
    body = b'{"user_id":"user-1"}'
    state = NonceState()
    authenticator = GatewayAuthenticator(SECRET, state)

    with pytest.raises(GatewayAuthenticationError):
        asyncio.run(
            authenticator.verify(Request(signed_headers(body)), body + b" ", "user-1")
        )
    with pytest.raises(GatewayAuthenticationError):
        asyncio.run(
            authenticator.verify(Request(signed_headers(body)), body, "user-2")
        )

    assert state.calls == []


def test_stale_timestamp_and_nonce_replay_are_rejected():
    body = b"{}"
    now = 1_786_659_200
    state = NonceState()
    authenticator = GatewayAuthenticator(
        SECRET, state, clock_skew_seconds=300, clock=lambda: now
    )

    with pytest.raises(GatewayAuthenticationError):
        asyncio.run(
            authenticator.verify(
                Request(signed_headers(body, timestamp=now - 301)), body, "user-1"
            )
        )

    headers = signed_headers(body, timestamp=now)
    asyncio.run(authenticator.verify(Request(headers), body, "user-1"))
    with pytest.raises(GatewayAuthenticationError):
        asyncio.run(authenticator.verify(Request(headers), body, "user-1"))


def test_header_grammar_is_strict_ascii_and_errors_do_not_echo_secrets():
    body = b"{}"
    for name, value in (
        ("X-Gateway-Timestamp", "+1786659200"),
        ("X-Gateway-Nonce", "短nonce"),
        ("X-Gateway-User-ID", "bad/user"),
        ("X-Gateway-Signature", "ABCDEF"),
    ):
        headers = signed_headers(body)
        headers[name] = value
        with pytest.raises(GatewayAuthenticationError) as raised:
            asyncio.run(
                GatewayAuthenticator(SECRET, NonceState()).verify(
                    Request(headers), body, "user-1"
                )
            )
        assert SECRET not in str(raised.value)
        assert value not in str(raised.value)


def test_signature_comparison_uses_compare_digest(monkeypatch):
    body = b"{}"
    calls = []
    original = hmac.compare_digest

    def recording_compare(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("xiaoliao_agent.gateway_auth.hmac.compare_digest", recording_compare)
    asyncio.run(
        GatewayAuthenticator(SECRET, NonceState()).verify(
            Request(signed_headers(body)), body, "user-1"
        )
    )

    assert any(len(left) == len(right) == 64 for left, right in calls)


class ApiRuntimeState(NonceState):
    async def ping(self):
        return True

    async def aclose(self):
        return None

    async def allow_rate(self, *args, **kwargs):
        return True


def api_body(user_id="user-1"):
    return (
        '{"user_id":"%s","session_id":"session-1","message":"hello",'
        '"context":{"consent":{"personalization":false},"user_summary":""},'
        '"debug":false}' % user_id
    ).encode("utf-8")


def api_signed_headers(
    body,
    *,
    signed_user_id="user-1",
    nonce="nonce-1234567890",
    method="POST",
    path="/v1/chat",
):
    timestamp = int(time.time())
    signature = sign_gateway_request(
        "g" * 48,
        timestamp,
        nonce,
        method,
        path,
        signed_user_id,
        hashlib.sha256(body).hexdigest(),
    )
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + "a" * 48,
        "X-Gateway-Timestamp": str(timestamp),
        "X-Gateway-Nonce": nonce,
        "X-Gateway-User-ID": signed_user_id,
        "X-Gateway-Signature": signature,
    }


def production_app(monkeypatch, runtime_state, agent):
    monkeypatch.setattr(api_server, "verify_schema", lambda _url: None)
    return create_app(
        lambda: agent,
        settings=production_settings(),
        runtime_state=runtime_state,
        user_data_service=UserDataService(MemoryUserRepository()),
    )


def test_production_user_route_requires_hmac_even_with_valid_bearer(monkeypatch):
    agent = RecordingAgent()
    with TestClient(production_app(monkeypatch, ApiRuntimeState(), agent)) as client:
        response = client.post(
            "/v1/chat",
            content=api_body(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + "a" * 48,
            },
        )

    assert response.status_code == 401
    assert response.json()["error_code"] == "AGENT_UNAUTHORIZED"
    assert agent.calls == 0


def test_production_signed_request_succeeds_and_replay_is_rejected(monkeypatch):
    body = api_body()
    agent = RecordingAgent()
    runtime_state = ApiRuntimeState()
    headers = api_signed_headers(body)
    with TestClient(production_app(monkeypatch, runtime_state, agent)) as client:
        accepted = client.post("/v1/chat", content=body, headers=headers)
        replay = client.post("/v1/chat", content=body, headers=headers)

    assert accepted.status_code == 200
    assert replay.status_code == 401
    assert agent.calls == 1


def test_production_hmac_does_not_replace_gateway_service_token(monkeypatch):
    body = api_body()
    agent = RecordingAgent()
    signed = api_signed_headers(body)
    signed.pop("Authorization")
    with TestClient(production_app(monkeypatch, ApiRuntimeState(), agent)) as client:
        response = client.post(
            "/v1/chat", content=body, headers=signed
        )

    assert response.status_code == 401
    assert agent.calls == 0


def test_production_signature_subject_must_match_body_user(monkeypatch):
    body = api_body("user-2")
    agent = RecordingAgent()
    headers = api_signed_headers(body, signed_user_id="user-1")
    with TestClient(production_app(monkeypatch, ApiRuntimeState(), agent)) as client:
        response = client.post("/v1/chat", content=body, headers=headers)

    assert response.status_code == 401
    assert agent.calls == 0


def test_development_user_route_keeps_service_token_compatibility():
    agent = RecordingAgent()
    with TestClient(
        create_app(lambda: agent, api_token="test-token", test_mode=True)
    ) as client:
        response = client.post(
            "/v1/chat",
            content=api_body(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-token",
            },
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "method,path,target,body",
    [
        (
            "POST",
            "/chat",
            "/chat",
            b'{"userId":"user-2","message":"hello","conversationHistory":[]}',
        ),
        ("POST", "/v1/chat/stream", "/v1/chat/stream", api_body("user-2")),
        (
            "POST",
            "/v1/users/consent",
            "/v1/users/consent",
            b'{"user_id":"user-2","personalization":true,"sensitive":false}',
        ),
        ("GET", "/v1/me/summary", "/v1/me/summary?user_id=user-2", b""),
        ("GET", "/v1/me/memories", "/v1/me/memories?user_id=user-2", b""),
        (
            "PATCH",
            "/v1/me/memories/memory-1",
            "/v1/me/memories/memory-1",
            b'{"user_id":"user-2","content":"cross-user"}',
        ),
        (
            "DELETE",
            "/v1/me/memories/memory-1",
            "/v1/me/memories/memory-1?user_id=user-2",
            b"",
        ),
        (
            "POST",
            "/v1/privacy/delete-request",
            "/v1/privacy/delete-request",
            b'{"user_id":"user-2"}',
        ),
        (
            "POST",
            "/v1/action-events",
            "/v1/action-events",
            b'{"event_id":"event-1","recommendation_id":"recommendation-1","user_id":"user-2","module":"M1","event_type":"completed","occurred_at":"2026-08-17T12:00:00+08:00","metadata":{}}',
        ),
    ],
)
def test_all_user_routes_reject_cross_user_gateway_subject(
    monkeypatch, method, path, target, body
):
    agent = RecordingAgent()
    headers = api_signed_headers(
        body,
        signed_user_id="user-1",
        nonce="nonce-route-1234567890",
        method=method,
        path=path,
    )
    headers["Authorization"] = "Bearer " + "a" * 48
    with TestClient(production_app(monkeypatch, ApiRuntimeState(), agent)) as client:
        response = client.request(method, target, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error_code"] == "AGENT_UNAUTHORIZED"
