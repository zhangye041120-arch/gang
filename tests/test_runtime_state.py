import asyncio

import fakeredis.aioredis
import pytest

from xiaoliao_agent.runtime_state import RedisRuntimeState, RuntimeSubjectDeleted


def make_states():
    server = fakeredis.FakeServer()
    first_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    second_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    first = RedisRuntimeState(
        first_client,
        prefix="test-m7",
        idempotency_ttl_seconds=600,
        execution_ttl_seconds=30,
        nonce_ttl_seconds=60,
    )
    second = RedisRuntimeState(
        second_client,
        prefix="test-m7",
        idempotency_ttl_seconds=600,
        execution_ttl_seconds=30,
        nonce_ttl_seconds=60,
    )
    return first, second, first_client


def test_nonce_is_shared_and_single_use():
    async def scenario():
        first, second, _client = make_states()
        assert await first.consume_nonce("nonce-1") is True
        assert await second.consume_nonce("nonce-1") is False
        await first.aclose()
        await second.aclose()

    asyncio.run(scenario())


def test_rate_limit_is_shared_across_clients():
    async def scenario():
        first, second, _client = make_states()
        assert await first.allow_rate("gateway", "subject-a", per_minute=2, per_user=2)
        assert await second.allow_rate("gateway", "subject-a", per_minute=2, per_user=2)
        assert not await first.allow_rate("gateway", "subject-a", per_minute=2, per_user=2)
        await first.aclose()
        await second.aclose()

    asyncio.run(scenario())


def test_idempotency_claim_finish_cache_and_conflict():
    async def scenario():
        first, second, _client = make_states()
        execute = await first.claim_idempotency("gateway", "idem-1", "fp-a", wait_timeout=0)
        assert execute.decision == "execute"
        assert execute.execution_token

        waiting = await second.claim_idempotency("gateway", "idem-1", "fp-a", wait_timeout=0)
        assert waiting.decision == "in_progress"
        assert await second.finish_idempotency(
            "gateway", "idem-1", "stale-token", 200, {"reply": "wrong"}
        ) is False
        assert await first.finish_idempotency(
            "gateway", "idem-1", execute.execution_token, 200, {"reply": "ok"}
        ) is True

        cached = await second.claim_idempotency("gateway", "idem-1", "fp-a", wait_timeout=0)
        assert cached.decision == "cached"
        assert cached.status_code == 200
        assert cached.response == {"reply": "ok"}
        conflict = await second.claim_idempotency("gateway", "idem-1", "fp-b", wait_timeout=0)
        assert conflict.decision == "conflict"
        await first.aclose()
        await second.aclose()

    asyncio.run(scenario())


def test_expired_execution_lease_can_be_reclaimed():
    async def scenario():
        first, second, client = make_states()
        original = await first.claim_idempotency("gateway", "idem-expired", "fp", wait_timeout=0)
        key = first.idempotency_key("gateway", "idem-expired")
        await client.hset(key, "lease_until_ms", "0")

        reclaimed = await second.claim_idempotency("gateway", "idem-expired", "fp", wait_timeout=0)

        assert reclaimed.decision == "execute"
        assert reclaimed.execution_token != original.execution_token
        assert not await first.finish_idempotency(
            "gateway", "idem-expired", original.execution_token, 200, {"stale": True}
        )
        await first.aclose()
        await second.aclose()

    asyncio.run(scenario())


def test_abandon_only_releases_owned_execution():
    async def scenario():
        first, _second, _client = make_states()
        claim = await first.claim_idempotency("gateway", "idem-abandon", "fp", wait_timeout=0)

        assert not await first.abandon_idempotency("gateway", "idem-abandon", "wrong")
        assert await first.abandon_idempotency(
            "gateway", "idem-abandon", claim.execution_token
        )
        retry = await first.claim_idempotency("gateway", "idem-abandon", "fp", wait_timeout=0)
        assert retry.decision == "execute"
        assert retry.execution_token != claim.execution_token
        await first.aclose()

    asyncio.run(scenario())


def test_subject_index_deletes_registered_keys_and_keeps_tombstone():
    async def scenario():
        first, _second, client = make_states()
        subject = "hmac-sha256:v1:abc"
        key_a = "test-m7:memory:a"
        key_b = "test-m7:action:b"
        await client.set(key_a, "1", ex=60)
        await client.set(key_b, "1", ex=60)
        await first.register_subject_key(subject, key_a)
        await first.register_subject_key(subject, key_b)
        assert await first.begin_deletion(subject)

        deleted = await first.delete_subject(subject)

        assert deleted == 2
        assert await client.exists(key_a, key_b) == 0
        assert await first.is_tombstoned(subject)
        await first.aclose()

    asyncio.run(scenario())


def test_subject_rate_and_idempotency_keys_are_automatically_indexed():
    async def scenario():
        first, _second, client = make_states()
        subject = "hmac-sha256:v1:" + "b" * 64
        assert await first.allow_rate(
            subject, "raw-user", per_minute=10, per_user=10
        )
        await first.claim_idempotency(
            subject, "indexed-idem", "fp", wait_timeout=0
        )
        index_key = first._subject_index_key(subject)
        indexed = await client.smembers(index_key)

        assert first.idempotency_key(subject, "indexed-idem") in indexed
        assert len(indexed) >= 3
        await first.begin_deletion(subject)
        await first.delete_subject(subject)
        assert await client.exists(*indexed) == 0
        assert await client.exists(index_key) == 0
        await first.aclose()

    asyncio.run(scenario())


def test_subject_tombstone_blocks_new_runtime_keys():
    async def scenario():
        first, _second, _client = make_states()
        subject = "hmac-sha256:v1:" + "c" * 64
        await first.begin_deletion(subject)

        with pytest.raises(RuntimeSubjectDeleted):
            await first.allow_rate(subject, "raw-user", per_minute=10, per_user=10)
        with pytest.raises(RuntimeSubjectDeleted):
            await first.claim_idempotency(subject, "new-idem", "fp", wait_timeout=0)
        with pytest.raises(RuntimeSubjectDeleted):
            await first.register_subject_key(subject, "new-key")
        await first.aclose()

    asyncio.run(scenario())


def test_all_runtime_keys_have_ttl():
    async def scenario():
        first, _second, client = make_states()
        await first.consume_nonce("nonce-ttl")
        await first.allow_rate("gateway", "subject", per_minute=10, per_user=10)
        await first.claim_idempotency("gateway", "idem-ttl", "fp", wait_timeout=0)
        keys = [key async for key in client.scan_iter(match="test-m7:*")]

        assert keys
        ttls = [await client.ttl(key) for key in keys]
        assert all(ttl > 0 for ttl in ttls)
        await first.aclose()

    asyncio.run(scenario())
