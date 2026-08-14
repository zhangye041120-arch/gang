from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import json
import secrets
import time
from typing import Any

import redis.asyncio as redis_async
from redis.exceptions import RedisError, WatchError


class RuntimeStateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimResult:
    decision: str
    execution_token: str = ""
    status_code: int | None = None
    response: dict[str, Any] | None = None


class RedisRuntimeState:
    def __init__(
        self,
        client,
        *,
        prefix: str = "xiaoliao",
        idempotency_ttl_seconds: int = 86_400,
        execution_ttl_seconds: int = 300,
        nonce_ttl_seconds: int = 900,
    ):
        self.client = client
        self.prefix = prefix.rstrip(":")
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.execution_ttl_seconds = execution_ttl_seconds
        self.nonce_ttl_seconds = nonce_ttl_seconds

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        prefix: str = "xiaoliao",
        idempotency_ttl_seconds: int = 86_400,
        execution_ttl_seconds: int = 300,
        nonce_ttl_seconds: int = 900,
    ) -> "RedisRuntimeState":
        if not url.strip():
            raise ValueError("Redis URL is required")
        client = redis_async.Redis.from_url(url, decode_responses=True)
        return cls(
            client,
            prefix=prefix,
            idempotency_ttl_seconds=idempotency_ttl_seconds,
            execution_ttl_seconds=execution_ttl_seconds,
            nonce_ttl_seconds=nonce_ttl_seconds,
        )

    @staticmethod
    def _digest(*parts: str) -> str:
        canonical = "\n".join(parts).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _key(self, kind: str, identifier: str) -> str:
        return f"{self.prefix}:{kind}:{identifier}"

    def idempotency_key(self, scope: str, key: str) -> str:
        return self._key("idempotency", self._digest(scope, key))

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis is unavailable") from exc

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()
            return
        await self.client.close()

    async def consume_nonce(self, nonce: str, ttl_seconds: int | None = None) -> bool:
        key = self._key("nonce", self._digest(nonce))
        try:
            return bool(
                await self.client.set(
                    key,
                    "1",
                    nx=True,
                    ex=ttl_seconds or self.nonce_ttl_seconds,
                )
            )
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis nonce storage is unavailable") from exc

    async def allow_rate(
        self,
        principal: str,
        user_id: str,
        *,
        per_minute: int,
        per_user: int,
    ) -> bool:
        window = str(int(time.time() // 60))
        principal_key = self._key("rate-principal", self._digest(principal, window))
        user_key = self._key("rate-user", self._digest(user_id, window))
        try:
            async with self.client.pipeline(transaction=True) as pipeline:
                pipeline.incr(principal_key)
                pipeline.expire(principal_key, 120)
                pipeline.incr(user_key)
                pipeline.expire(user_key, 120)
                principal_count, _p_expire, user_count, _u_expire = await pipeline.execute()
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis rate limiting is unavailable") from exc
        return int(principal_count) <= per_minute and int(user_count) <= per_user

    async def claim_idempotency(
        self,
        scope: str,
        key: str,
        fingerprint: str,
        *,
        wait_timeout: float = 5.0,
    ) -> ClaimResult:
        redis_key = self.idempotency_key(scope, key)
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while True:
            token = secrets.token_urlsafe(24)
            now_ms = int(time.time() * 1000)
            lease_until_ms = now_ms + self.execution_ttl_seconds * 1000
            try:
                async with self.client.pipeline(transaction=True) as pipeline:
                    await pipeline.watch(redis_key)
                    entry = await pipeline.hgetall(redis_key)
                    if not entry:
                        pipeline.multi()
                        pipeline.hset(
                            redis_key,
                            mapping={
                                "fingerprint": fingerprint,
                                "state": "executing",
                                "execution_token": token,
                                "lease_until_ms": str(lease_until_ms),
                            },
                        )
                        pipeline.expire(redis_key, self.idempotency_ttl_seconds)
                        await pipeline.execute()
                        return ClaimResult("execute", execution_token=token)
                    if entry.get("fingerprint") != fingerprint:
                        await pipeline.unwatch()
                        return ClaimResult("conflict")
                    if entry.get("state") == "done":
                        await pipeline.unwatch()
                        body = json.loads(entry.get("body") or "{}")
                        return ClaimResult(
                            "cached",
                            status_code=int(entry.get("status_code") or 200),
                            response=body,
                        )
                    if int(entry.get("lease_until_ms") or 0) <= now_ms:
                        pipeline.multi()
                        pipeline.hset(
                            redis_key,
                            mapping={
                                "state": "executing",
                                "execution_token": token,
                                "lease_until_ms": str(lease_until_ms),
                            },
                        )
                        pipeline.expire(redis_key, self.idempotency_ttl_seconds)
                        await pipeline.execute()
                        return ClaimResult("execute", execution_token=token)
                    await pipeline.unwatch()
            except WatchError:
                continue
            except (RedisError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeStateUnavailable("Redis idempotency state is unavailable") from exc
            if time.monotonic() >= deadline:
                return ClaimResult("in_progress")
            await asyncio.sleep(0.05)

    async def finish_idempotency(
        self,
        scope: str,
        key: str,
        execution_token: str,
        status_code: int,
        body: dict[str, Any],
    ) -> bool:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65_536:
            raise ValueError("idempotency response is too large")
        redis_key = self.idempotency_key(scope, key)
        while True:
            try:
                async with self.client.pipeline(transaction=True) as pipeline:
                    await pipeline.watch(redis_key)
                    entry = await pipeline.hgetall(redis_key)
                    if (
                        not entry
                        or entry.get("state") != "executing"
                        or entry.get("execution_token") != execution_token
                    ):
                        await pipeline.unwatch()
                        return False
                    pipeline.multi()
                    pipeline.hset(
                        redis_key,
                        mapping={
                            "state": "done",
                            "status_code": str(status_code),
                            "body": encoded,
                        },
                    )
                    pipeline.hdel(redis_key, "execution_token", "lease_until_ms")
                    pipeline.expire(redis_key, self.idempotency_ttl_seconds)
                    await pipeline.execute()
                    return True
            except WatchError:
                continue
            except RedisError as exc:
                raise RuntimeStateUnavailable("Redis idempotency state is unavailable") from exc

    async def abandon_idempotency(
        self,
        scope: str,
        key: str,
        execution_token: str,
    ) -> bool:
        redis_key = self.idempotency_key(scope, key)
        while True:
            try:
                async with self.client.pipeline(transaction=True) as pipeline:
                    await pipeline.watch(redis_key)
                    entry = await pipeline.hgetall(redis_key)
                    if (
                        not entry
                        or entry.get("state") != "executing"
                        or entry.get("execution_token") != execution_token
                    ):
                        await pipeline.unwatch()
                        return False
                    pipeline.multi()
                    pipeline.delete(redis_key)
                    await pipeline.execute()
                    return True
            except WatchError:
                continue
            except RedisError as exc:
                raise RuntimeStateUnavailable("Redis idempotency state is unavailable") from exc

    def _subject_index_key(self, subject_hmac: str) -> str:
        return self._key("subject-keys", self._digest(subject_hmac))

    def _tombstone_key(self, subject_hmac: str) -> str:
        return self._key("subject-deleted", self._digest(subject_hmac))

    async def register_subject_key(self, subject_hmac: str, key: str) -> None:
        if await self.is_tombstoned(subject_hmac):
            raise ValueError("subject is deleted")
        index_key = self._subject_index_key(subject_hmac)
        try:
            async with self.client.pipeline(transaction=True) as pipeline:
                pipeline.sadd(index_key, key)
                pipeline.expire(index_key, self.idempotency_ttl_seconds)
                await pipeline.execute()
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis subject index is unavailable") from exc

    async def begin_deletion(self, subject_hmac: str, ttl_seconds: int = 2_592_000) -> bool:
        try:
            return bool(
                await self.client.set(
                    self._tombstone_key(subject_hmac),
                    "1",
                    nx=True,
                    ex=ttl_seconds,
                )
            )
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis deletion state is unavailable") from exc

    async def is_tombstoned(self, subject_hmac: str) -> bool:
        try:
            return bool(await self.client.exists(self._tombstone_key(subject_hmac)))
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis deletion state is unavailable") from exc

    async def delete_subject(self, subject_hmac: str) -> int:
        index_key = self._subject_index_key(subject_hmac)
        try:
            keys = list(await self.client.smembers(index_key))
            deleted = int(await self.client.delete(*keys)) if keys else 0
            await self.client.delete(index_key)
            return deleted
        except RedisError as exc:
            raise RuntimeStateUnavailable("Redis subject cleanup is unavailable") from exc
