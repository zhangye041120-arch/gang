"""WeCom internal group robot sender.

Enterprise WeChat group robots (消息推送) send to internal employee groups
only. Unlike the customer-service channel, there is no 48-hour reply window,
so scheduled daily reminders are allowed here.
"""

from __future__ import annotations

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
