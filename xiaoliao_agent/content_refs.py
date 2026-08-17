from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import re


_KEY_VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_PURPOSE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class HmacReferenceService:
    __slots__ = ("__previous_keys", "__secret", "key_version")

    def __init__(
        self,
        secret: str,
        *,
        key_version: str = "v1",
        previous_keys: Mapping[str, str] | None = None,
    ):
        if not secret:
            raise ValueError("HMAC reference secret is required")
        if not _KEY_VERSION.fullmatch(key_version):
            raise ValueError("HMAC key version is invalid")
        if previous_keys is not None and not isinstance(previous_keys, Mapping):
            raise ValueError("HMAC previous keys must be a mapping")

        previous_items = tuple((previous_keys or {}).items())
        seen_secrets = {secret}
        encoded_previous_keys: list[tuple[str, bytes]] = []
        for previous_version, previous_secret in previous_items:
            if (
                not isinstance(previous_version, str)
                or not _KEY_VERSION.fullmatch(previous_version)
            ):
                raise ValueError("HMAC previous key version is invalid")
            if previous_version == key_version:
                raise ValueError("HMAC current key version cannot be a previous key")
            if not isinstance(previous_secret, str) or not previous_secret:
                raise ValueError("HMAC previous key secret is invalid")
            if previous_secret in seen_secrets:
                raise ValueError("HMAC key secrets must be unique")
            seen_secrets.add(previous_secret)
            encoded_previous_keys.append(
                (previous_version, previous_secret.encode("utf-8"))
            )

        self.__secret = secret.encode("utf-8")
        self.__previous_keys = tuple(encoded_previous_keys)
        self.key_version = key_version

    def __repr__(self) -> str:
        return f"HmacReferenceService(key_version={self.key_version!r})"

    @staticmethod
    def _reference_with_key(
        secret: bytes,
        key_version: str,
        purpose: str,
        value: bytes,
    ) -> str:
        if not _PURPOSE.fullmatch(purpose):
            raise ValueError("HMAC reference purpose is invalid")
        message = purpose.encode("ascii") + b"\0" + value
        digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{key_version}:{digest}"

    def _reference(self, purpose: str, value: bytes) -> str:
        return self._reference_with_key(
            self.__secret,
            self.key_version,
            purpose,
            value,
        )

    def subject_hmac(self, user_id: str) -> str:
        return self._reference("subject", user_id.encode("utf-8"))

    def subject_hmac_candidates(self, user_id: str) -> tuple[str, ...]:
        encoded_user_id = user_id.encode("utf-8")
        return (
            self._reference("subject", encoded_user_id),
            *(
                self._reference_with_key(
                    secret,
                    version,
                    "subject",
                    encoded_user_id,
                )
                for version, secret in self.__previous_keys
            ),
        )

    def conversation_ref(self, content: str) -> str:
        return self._reference("conversation", content.encode("utf-8"))

    def candidate_reply_ref(self, content: str) -> str:
        return self._reference("candidate-reply", content.encode("utf-8"))

    def fingerprint(self, purpose: str, canonical: bytes) -> str:
        if not _PURPOSE.fullmatch(purpose):
            raise ValueError("HMAC reference purpose is invalid")
        return self._reference("fingerprint:" + purpose, canonical)

    def fingerprint_candidates(
        self,
        purpose: str,
        canonical: bytes,
    ) -> tuple[str, ...]:
        if not _PURPOSE.fullmatch(purpose):
            raise ValueError("HMAC reference purpose is invalid")
        domain = "fingerprint:" + purpose
        return (
            self._reference(domain, canonical),
            *(
                self._reference_with_key(secret, version, domain, canonical)
                for version, secret in self.__previous_keys
            ),
        )
