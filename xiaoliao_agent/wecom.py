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
