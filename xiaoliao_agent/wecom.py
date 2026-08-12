"""WeCom inbound adapter boundary.

This module only accepts verified/decrypted events and produces a chat response
payload. It never sends replies; Java/WeCom remains the sending layer.
"""

from datetime import datetime, timezone
import hashlib
import hmac
from threading import Lock
from typing import Any, Callable


class WeComError(Exception):
    pass


class WeComSignatureError(WeComError):
    pass


class WeComCryptoError(WeComError):
    pass


class WeComEventError(WeComError):
    pass


def verify_callback_signature(
    token: str,
    timestamp: str,
    nonce: str,
    echostr: str,
    signature: str,
    *,
    now: datetime | None = None,
    max_skew_seconds: int = 300,
) -> bool:
    try:
        event_time = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise WeComSignatureError("callback timestamp is invalid") from exc
    current = now or datetime.now(timezone.utc)
    if abs((current - event_time).total_seconds()) > max_skew_seconds:
        raise WeComSignatureError("callback timestamp is outside allowed window")
    expected = hashlib.sha1(
        "".join(sorted([token, str(timestamp), nonce, echostr])).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def decrypt_callback(payload: str, decryptor: Callable[[str], str]) -> str:
    try:
        return decryptor(payload)
    except Exception as exc:
        raise WeComCryptoError("callback payload decryption failed") from exc


class MessageDedupStore:
    def __init__(self, retention_seconds: float = 7 * 24 * 3600):
        self.retention_seconds = max(60, float(retention_seconds))
        self._entries: dict[str, float] = {}
        self._lock = Lock()

    def _cleanup(self, now: datetime) -> None:
        cutoff = now.timestamp() - self.retention_seconds
        self._entries = {
            event_id: stamp for event_id, stamp in self._entries.items() if stamp > cutoff
        }

    def contains(self, event_id: str, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        with self._lock:
            self._cleanup(current)
            return event_id in self._entries

    def mark(self, event_id: str, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        with self._lock:
            self._cleanup(current)
            self._entries[event_id] = current.timestamp()


class MessageOrderStore:
    def __init__(self):
        self._last: dict[str, int] = {}
        self._lock = Lock()

    def record(self, user_id: str, sequence: int) -> None:
        with self._lock:
            self._last[user_id] = max(self._last.get(user_id, sequence), sequence)

    def is_out_of_order(self, user_id: str, sequence: int) -> bool:
        with self._lock:
            last = self._last.get(user_id)
            if last is not None and sequence <= last:
                return True
            self._last[user_id] = sequence
            return False


class WeComChatAdapter:
    """Maps an inbound event to /v1/chat; returns a response payload only."""

    def __init__(
        self,
        chat_client: Callable[..., dict[str, Any]],
        *,
        dedup: MessageDedupStore | None = None,
        order: MessageOrderStore | None = None,
    ):
        self.chat_client = chat_client
        self.dedup = dedup or MessageDedupStore()
        self.order = order or MessageOrderStore()

    def handle_message(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            raise WeComEventError("event_id is required")
        if self.dedup.contains(event_id):
            return {"status": "duplicate"}
        user_id = str(event.get("user_id", "")).strip()
        sequence = event.get("sequence")
        if sequence is not None and self.order.is_out_of_order(user_id, int(sequence)):
            return {"status": "out_of_order"}
        try:
            response = self.chat_client(
                message=str(event.get("message", "")),
                user_id=user_id,
                session_id=str(event.get("session_id", "")),
                context=event.get("context", {}),
                idempotency_key=event_id,
            )
        except TimeoutError:
            return {"status": "chat_failed", "error_code": "EVAL_TIMEOUT"}
        except Exception as exc:
            return {
                "status": "chat_failed",
                "error_code": "EVAL_REQUEST_FAILED",
                "detail": str(exc)[:500],
            }
        self.dedup.mark(event_id)
        return {"status": "ok", **response}


"""WeCom internal group robot sender.

Enterprise WeChat group robots (消息推送) send to internal employee groups
only. Unlike the customer-service channel, there is no 48-hour reply window,
so scheduled daily reminders are allowed here.
"""


import base64
import hashlib
import hmac
import time as time_module
from urllib.parse import urlencode
from typing import Any, Iterable

import httpx


class WeComWebhookError(Exception):
    """Raised when the group robot webhook call fails."""


class WeComAppMessageError(Exception):
    """Raised when the Enterprise WeChat application message call fails."""


def truncate_utf8_bytes(text: str, max_bytes: int = 2048) -> str:
    """Truncate *text* so its UTF-8 encoding fits inside *max_bytes*."""
    encoded = (text or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def wecom_webhook_signature(secret: str, timestamp: int | None = None) -> tuple[int, str]:
    """Return ``(timestamp, sign)`` using the group robot 加签 algorithm."""
    if not secret:
        raise WeComWebhookError("webhook secret is empty")
    ts = timestamp if timestamp is not None else int(time_module.time())
    message = f"{ts}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return ts, base64.b64encode(digest).decode("ascii")


def append_webhook_signature(
    webhook_url: str,
    secret: str,
    timestamp: int | None = None,
) -> str:
    if not secret:
        return webhook_url
    ts, sign = wecom_webhook_signature(secret, timestamp)
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}{urlencode({'timestamp': ts, 'sign': sign})}"


class WeComWebhookSender:
    """Sends text messages to an Enterprise WeChat internal group robot."""

    def __init__(
        self,
        webhook_url: str,
        *,
        secret: str = "",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ):
        self.webhook_url = (webhook_url or "").strip()
        self.secret = (secret or "").strip()
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._client = client

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = append_webhook_signature(
            self.webhook_url,
            self.secret,
            int(time_module.time()),
        )
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, timeout=self.timeout_seconds)
            else:
                response = httpx.post(url, json=payload, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise WeComWebhookError(f"webhook request failed: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise WeComWebhookError(
                f"webhook returned non-JSON response: HTTP {response.status_code}"
            ) from exc
        if not isinstance(body, dict) or body.get("errcode", 0) != 0:
            raise WeComWebhookError(
                "webhook rejected: "
                f"{body.get('errcode') if isinstance(body, dict) else 'unknown'} "
                f"{body.get('errmsg', '') if isinstance(body, dict) else ''}"
            )
        return body

    def send_text(
        self,
        content: str,
        *,
        mentioned_user_ids: Iterable[str] = (),
        mentioned_mobiles: Iterable[str] = (),
        mention_all: bool = False,
    ) -> dict[str, Any]:
        if not self.webhook_url:
            raise WeComWebhookError("webhook_url is not configured")
        text: dict[str, Any] = {"content": truncate_utf8_bytes(content or "")}
        user_ids = [item for item in mentioned_user_ids if item]
        mobiles = [item for item in mentioned_mobiles if item]
        if mention_all:
            user_ids.append("@all")
        if user_ids:
            text["mentioned_list"] = user_ids
        if mobiles:
            text["mentioned_mobile_list"] = mobiles
        return self._post({"msgtype": "text", "text": text})


class WeComAppMessageSender:
    """Sends private text messages to Enterprise WeChat employees.

    Uses the self-built application message API.  Internal employees have no
    48-hour customer-service window, so scheduled private reminders are allowed.
    """

    INVALID_TOKEN_CODES = {40014, 42001}

    def __init__(
        self,
        corp_id: str,
        agent_id: str | int,
        agent_secret: str,
        *,
        base_url: str = "https://qyapi.weixin.qq.com",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ):
        self.corp_id = (corp_id or "").strip()
        self.agent_id = agent_id
        self.agent_secret = (agent_secret or "").strip()
        self.base_url = (base_url or "https://qyapi.weixin.qq.com").rstrip("/")
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._client = client
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            if self._client is not None:
                response = self._client.request(
                    method,
                    url,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
            else:
                response = httpx.request(
                    method,
                    url,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise WeComAppMessageError(f"wecom api request failed: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise WeComAppMessageError(
                f"wecom api returned non-JSON response: HTTP {response.status_code}"
            ) from exc
        if not isinstance(body, dict):
            raise WeComAppMessageError("wecom api returned a non-object response")
        return body

    def _get_access_token(self) -> str:
        now = time_module.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        body = self._request(
            "GET",
            "/cgi-bin/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.agent_secret},
        )
        if body.get("errcode", 0) != 0:
            raise WeComAppMessageError(
                "wecom gettoken failed: "
                f"{body.get('errcode')} {body.get('errmsg', '')}"
            )
        self._token = str(body.get("access_token", ""))
        self._token_expires_at = now + int(body.get("expires_in", 7200))
        return self._token

    def _send_with_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = self._get_access_token()
        body = self._request(
            "POST",
            "/cgi-bin/message/send",
            params={"access_token": token},
            json=payload,
        )
        if body.get("errcode") in self.INVALID_TOKEN_CODES:
            self._token = None
            token = self._get_access_token()
            body = self._request(
                "POST",
                "/cgi-bin/message/send",
                params={"access_token": token},
                json=payload,
            )
        if body.get("errcode", 0) != 0:
            raise WeComAppMessageError(
                "wecom message/send failed: "
                f"{body.get('errcode')} {body.get('errmsg', '')}"
            )
        return body

    def send_text(self, user_id: str, content: str) -> dict[str, Any]:
        if not self.corp_id or not self.agent_id or not self.agent_secret:
            raise WeComAppMessageError("wecom app credentials are not configured")
        user_id = (user_id or "").strip()
        if not user_id:
            raise WeComAppMessageError("touser is empty")
        try:
            agent_id = int(self.agent_id)
        except (TypeError, ValueError) as exc:
            raise WeComAppMessageError("agent_id must be an integer") from exc
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": agent_id,
            "text": {"content": truncate_utf8_bytes(content or "")},
        }
        return self._send_with_token(payload)
