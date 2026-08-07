import json

import httpx
import pytest

from xiaoliao_agent.client import (
    ModelHTTPError,
    ModelInvalidResponseError,
    ModelNetworkError,
    ModelRateLimitError,
    ModelTimeoutError,
    OpenAICompatibleClient,
)
from xiaoliao_agent.config import Settings


def make_client(transport):
    return OpenAICompatibleClient(
        "https://model.invalid/v1",
        "secret-key",
        "test-model",
        Settings(),
        transport=transport,
    )


@pytest.mark.parametrize(("error", "expected_type", "expected_code"), [
    (httpx.ReadTimeout("slow"), ModelTimeoutError, "AGENT_MODEL_TIMEOUT"),
    (httpx.ConnectError("offline"), ModelNetworkError, "AGENT_MODEL_NETWORK"),
    (httpx.ConnectTimeout("connect timeout"), ModelTimeoutError, "AGENT_MODEL_TIMEOUT"),
])
def test_client_classifies_network_errors_without_leaking_credentials(error, expected_type, expected_code):
    def handler(request):
        raise error

    transport = httpx.MockTransport(handler)
    with pytest.raises(expected_type) as exc_info:
        make_client(transport).chat([{"role": "user", "content": "hello"}])
    assert exc_info.value.code == expected_code
    assert exc_info.value.request_id
    assert "secret-key" not in str(exc_info.value)


@pytest.mark.parametrize(("status", "expected_type", "expected_code"), [
    (429, ModelRateLimitError, "AGENT_MODEL_RATE_LIMITED"),
    (500, ModelHTTPError, "AGENT_MODEL_HTTP"),
])
def test_client_classifies_http_errors_without_leaking_credentials(status, expected_type, expected_code):
    def handler(request):
        return httpx.Response(status, json={"error": "secret body"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(expected_type) as exc_info:
        make_client(transport).chat([{"role": "user", "content": "hello"}])
    assert exc_info.value.code == expected_code
    assert exc_info.value.request_id
    assert "secret-key" not in str(exc_info.value)
    assert "secret body" not in str(exc_info.value)


def test_client_classifies_invalid_supplier_response():
    def handler(request):
        return httpx.Response(200, content=b"not-json")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ModelInvalidResponseError) as exc_info:
        make_client(transport).chat([{"role": "user", "content": "hello"}])
    assert exc_info.value.code == "AGENT_MODEL_INVALID_RESPONSE"


def test_client_keeps_supplier_usage_without_estimating_cost():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    transport = httpx.MockTransport(handler)
    client = make_client(transport)
    assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert client.last_usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_client_accepts_max_tokens_override():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(httpx.MockTransport(handler))
    assert client.max_tokens == 400
    custom = OpenAICompatibleClient(
        "https://model.invalid/v1",
        "secret-key",
        "test-model",
        Settings(),
        transport=httpx.MockTransport(handler),
        max_tokens=256,
    )
    assert custom.max_tokens == 256


def test_client_sends_enable_thinking_only_when_configured():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    default_client = make_client(httpx.MockTransport(handler))
    default_client.chat([{"role": "user", "content": "hello"}])
    assert "enable_thinking" not in seen["payload"]

    off_client = OpenAICompatibleClient(
        "https://model.invalid/v1",
        "secret-key",
        "test-model",
        Settings(),
        transport=httpx.MockTransport(handler),
        enable_thinking=False,
    )
    off_client.chat([{"role": "user", "content": "hello"}])
    assert seen["payload"]["enable_thinking"] is False

    on_client = OpenAICompatibleClient(
        "https://model.invalid/v1",
        "secret-key",
        "test-model",
        Settings(),
        transport=httpx.MockTransport(handler),
        enable_thinking=True,
    )
    on_client.chat([{"role": "user", "content": "hello"}])
    assert seen["payload"]["enable_thinking"] is True
