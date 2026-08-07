"""OpenAI-compatible model client with httpx connection pooling and streaming.

Replaces per-request urllib with a persistent httpx.Client so TCP+TLS
handshakes are amortised across turns.  Adds a stream() generator for
SSE token delivery.
"""

from __future__ import annotations

import json
from typing import Any, Generator

import httpx

from .config import Settings


class ModelClientError(RuntimeError):
    code = "AGENT_MODEL_UNAVAILABLE"

    def __init__(self, message: str, *, request_id: str):
        super().__init__(message)
        self.request_id = request_id


class ModelTimeoutError(ModelClientError):
    code = "AGENT_MODEL_TIMEOUT"


class ModelNetworkError(ModelClientError):
    code = "AGENT_MODEL_NETWORK"


class ModelHTTPError(ModelClientError):
    code = "AGENT_MODEL_HTTP"


class ModelRateLimitError(ModelClientError):
    code = "AGENT_MODEL_RATE_LIMITED"


class ModelInvalidResponseError(ModelClientError):
    code = "AGENT_MODEL_INVALID_RESPONSE"


class OpenAICompatibleClient:
    """Persistent HTTP client with connection pooling for one model provider.

    Keeps a module-level ``_client`` dict keyed by base_url so agents that
    share a provider reuse the same pool (e.g. when embedding + chat both
    call Qwen).
    """

    _clients: dict[str, httpx.Client] = {}

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        settings: Settings,
        transport: Any | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = settings.timeout_seconds
        self.stream_timeout = settings.stream_timeout_seconds
        self.temperature = settings.temperature
        self.max_tokens = settings.max_tokens if max_tokens is None else max_tokens
        self.enable_thinking = enable_thinking
        self.last_request_id: str | None = None
        self.last_usage: dict[str, int] | None = None
        self._http = self._get_client(self.base_url, transport=transport)

    @classmethod
    def _get_client(cls, base_url: str, transport: Any | None = None) -> httpx.Client:
        key = base_url.rstrip("/")
        if transport is not None:
            return httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=20),
            )
        if key not in cls._clients:
            cls._clients[key] = httpx.Client(
                http2=False,
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=20),
            )
        return cls._clients[key]

    @classmethod
    def close_all(cls) -> None:
        for client in cls._clients.values():
            client.close()
        cls._clients.clear()

    def _headers(self, request_id: str, *, stream: bool = False) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Request-ID": request_id,
        }
        if not stream:
            h["Accept"] = "application/json"
        return h

    def _payload(self, messages: list[dict[str, str]], *, json_mode: bool = False, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream"] = True
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        return payload

    def _raise_from_status(self, status: int, request_id: str) -> None:
        if status == 429:
            raise ModelRateLimitError("model rate limited", request_id=request_id)
        if status in {408, 504}:
            raise ModelTimeoutError("model request timed out", request_id=request_id)
        raise ModelHTTPError(f"model HTTP status {status}", request_id=request_id)

    # ------------------------------------------------------------------
    # non-streaming (backwards compatible)
    # ------------------------------------------------------------------
    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        import uuid

        request_id = uuid.uuid4().hex
        self.last_request_id = request_id
        self.last_usage = None

        try:
            response = self._http.post(
                f"{self.base_url}/chat/completions",
                json=self._payload(messages, json_mode=json_mode),
                headers=self._headers(request_id),
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("model request timed out", request_id=request_id) from exc
        except httpx.NetworkError as exc:
            raise ModelNetworkError("model network unavailable", request_id=request_id) from exc

        resolved_id = response.headers.get("x-request-id", request_id)
        self.last_request_id = resolved_id

        if response.status_code != 200:
            self._raise_from_status(response.status_code, resolved_id)

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModelInvalidResponseError("model response is not valid JSON", request_id=resolved_id) from exc

        return self._extract_content(body, resolved_id)

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    def chat_stream(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> Generator[str, None, None]:
        """Yield content tokens as they arrive (SSE).

        The final yielded value is a sentinel ``"__XLM_USAGE__:<json>"`` with
        token usage info so callers can reconstruct the same metadata that
        ``.chat()`` provides.
        """
        import uuid

        request_id = uuid.uuid4().hex
        self.last_request_id = request_id
        self.last_usage = None
        accumulated: list[str] = []

        try:
            with self._http.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=self._payload(messages, json_mode=json_mode, stream=True),
                headers=self._headers(request_id, stream=True),
                timeout=httpx.Timeout(self.stream_timeout, connect=10.0),
            ) as response:
                resolved_id = response.headers.get("x-request-id", request_id)
                self.last_request_id = resolved_id

                if response.status_code != 200:
                    # Read error body then raise
                    try:
                        body = response.read().decode(errors="replace")
                    except Exception:
                        body = ""
                    self._raise_from_status(response.status_code, resolved_id)

                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    token = delta.get("content", "")
                    if token:
                        accumulated.append(token)
                        yield token
                    # Capture usage from final chunk
                    usage = chunk.get("usage")
                    if usage and isinstance(usage, dict):
                        self.last_usage = {
                            "input_tokens": usage.get("prompt_tokens"),
                            "output_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                        }
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("model stream timed out", request_id=self.last_request_id or request_id) from exc
        except httpx.NetworkError as exc:
            raise ModelNetworkError("model network unavailable", request_id=self.last_request_id or request_id) from exc

        full = "".join(accumulated)
        yield f"__XLM_USAGE__:{json.dumps(self.last_usage or {}, ensure_ascii=False)}"

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _extract_content(self, body: dict[str, Any], request_id: str) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelInvalidResponseError("model response contract is invalid", request_id=request_id) from exc
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str):
            raise ModelInvalidResponseError("model content type is invalid", request_id=request_id)
        usage = body.get("usage")
        if isinstance(usage, dict):
            values = {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
            if all(value is None or isinstance(value, int) for value in values.values()):
                self.last_usage = values
        return content
