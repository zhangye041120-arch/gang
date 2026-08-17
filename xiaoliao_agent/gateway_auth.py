from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
import time
from typing import Any, Callable

from .content_refs import HmacReferenceService


_NONCE = re.compile(r"^[A-Za-z0-9_.:@-]{16,128}$")
_USER_ID = re.compile(r"^[A-Za-z0-9_:@.-]{1,64}$")
_PATH = re.compile(r"^/[A-Za-z0-9_./~%:@-]{0,511}$")
_BODY_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")


class GatewayAuthenticationError(Exception):
    def __init__(self):
        super().__init__("gateway authentication failed")


@dataclass(frozen=True)
class GatewayPrincipal:
    user_id: str
    subject_hmac: str


def canonical_gateway_message(
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    user_id: str,
    body_sha256: str,
) -> bytes:
    normalized_method = method.upper()
    if not (1_000_000_000 <= timestamp <= 99_999_999_999):
        raise ValueError("gateway timestamp is invalid")
    if not _NONCE.fullmatch(nonce):
        raise ValueError("gateway nonce is invalid")
    if normalized_method not in {"GET", "POST", "PATCH", "DELETE"}:
        raise ValueError("gateway method is invalid")
    if not _PATH.fullmatch(path):
        raise ValueError("gateway path is invalid")
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("gateway user is invalid")
    if not _BODY_SHA256.fullmatch(body_sha256):
        raise ValueError("gateway body digest is invalid")
    return "\n".join(
        (str(timestamp), nonce, normalized_method, path, user_id, body_sha256)
    ).encode("ascii")


def sign_gateway_request(
    secret: str,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    user_id: str,
    body_sha256: str,
) -> str:
    if not secret:
        raise ValueError("gateway HMAC secret is required")
    message = canonical_gateway_message(
        timestamp, nonce, method, path, user_id, body_sha256
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class GatewayAuthenticator:
    __slots__ = (
        "__secret",
        "runtime_state",
        "clock_skew_seconds",
        "clock",
        "reference_service",
    )

    def __init__(
        self,
        secret: str,
        runtime_state: Any,
        *,
        clock_skew_seconds: int = 300,
        clock: Callable[[], float] = time.time,
        reference_service: HmacReferenceService | None = None,
    ):
        if not secret:
            raise ValueError("gateway HMAC secret is required")
        if clock_skew_seconds < 1:
            raise ValueError("gateway clock skew must be positive")
        self.__secret = secret.encode("utf-8")
        self.runtime_state = runtime_state
        self.clock_skew_seconds = clock_skew_seconds
        self.clock = clock
        self.reference_service = reference_service or HmacReferenceService(secret)

    def __repr__(self) -> str:
        return f"GatewayAuthenticator(clock_skew_seconds={self.clock_skew_seconds})"

    async def verify(
        self,
        request: Any,
        body: bytes,
        expected_user_id: str,
    ) -> GatewayPrincipal:
        try:
            timestamp_raw = request.headers.get("X-Gateway-Timestamp", "")
            nonce = request.headers.get("X-Gateway-Nonce", "")
            user_id = request.headers.get("X-Gateway-User-ID", "")
            supplied_signature = request.headers.get("X-Gateway-Signature", "")
            if not re.fullmatch(r"[0-9]{10,11}", timestamp_raw):
                raise GatewayAuthenticationError()
            if not _NONCE.fullmatch(nonce):
                raise GatewayAuthenticationError()
            if not _USER_ID.fullmatch(user_id):
                raise GatewayAuthenticationError()
            if not _SIGNATURE.fullmatch(supplied_signature):
                raise GatewayAuthenticationError()
            timestamp = int(timestamp_raw)
            if abs(int(self.clock()) - timestamp) > self.clock_skew_seconds:
                raise GatewayAuthenticationError()
            if not hmac.compare_digest(user_id, expected_user_id):
                raise GatewayAuthenticationError()
            body_sha256 = hashlib.sha256(body).hexdigest()
            message = canonical_gateway_message(
                timestamp,
                nonce,
                request.method,
                request.url.path,
                user_id,
                body_sha256,
            )
            expected_signature = hmac.new(
                self.__secret, message, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise GatewayAuthenticationError()
        except GatewayAuthenticationError:
            raise
        except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
            raise GatewayAuthenticationError() from exc

        if not await self.runtime_state.consume_nonce(
            nonce, ttl_seconds=2 * self.clock_skew_seconds
        ):
            raise GatewayAuthenticationError()
        return GatewayPrincipal(
            user_id=user_id,
            subject_hmac=self.reference_service.subject_hmac(user_id),
        )
