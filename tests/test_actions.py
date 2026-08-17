from datetime import datetime, timedelta, timezone
import json

import pytest

from xiaoliao_agent.actions import (
    ActionAlreadyRecommended,
    ActionContractError,
    ActionEvent,
    ActionEventConflict,
    ActionMemoryUnavailable,
    ActionRecommendation,
    ActionService,
    MemoryActionRepository,
)
from xiaoliao_agent.memory import MemoryCandidate, MemoryService
from xiaoliao_agent.memory import MemoryMemoryRepository
from xiaoliao_agent.api_contract import ActionPayload
from xiaoliao_agent.content_refs import HmacReferenceService


def valid_action(module="M1", **overrides):
    data = {
        "type": "miniprogram",
        "module": module,
        "page": {
            "M1": "/pages/checkin/index",
            "M2": "/pages/games/index",
            "M3": "/pages/exercise/index",
            "M5": "/pages/community/index",
        }[module],
        "params": {},
        "reason": "invite_small_action",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("module", ["M1", "M2", "M3", "M5"])
def test_all_four_action_modules_validate(module):
    assert ActionPayload.model_validate(valid_action(module)).module == module


@pytest.mark.parametrize("action", [
    {**valid_action(), "module": "M9"},
    {**valid_action(), "page": "https://evil.example"},
    {**valid_action(), "params": {"url": "https://evil.example"}},
    {**valid_action(), "extra": "reject"},
])
def test_action_contract_rejects_unsafe_payloads(action):
    with pytest.raises(Exception):
        ActionPayload.model_validate(action)


def test_recommendation_state_machine_and_duplicate_events_are_idempotent():
    repository = MemoryActionRepository()
    service = ActionService(repository)
    recommendation = service.recommend("u1", "s1", valid_action(), source_message_id="msg-1")
    assert recommendation.status == "recommended"
    accepted = service.record_event(ActionEvent("evt-1", recommendation.recommendation_id, "u1", "M1", "accepted", datetime.now(timezone.utc), {}))
    assert accepted.status == "accepted"
    completed_event = ActionEvent("evt-2", recommendation.recommendation_id, "u1", "M1", "completed", datetime.now(timezone.utc), {"activity": "情绪签到", "effort": "完成了今天的记录"})
    completed = service.record_event(completed_event)
    assert completed.status == "completed"
    duplicate = service.record_event(completed_event)
    assert duplicate.status == "completed"
    with pytest.raises(ActionEventConflict):
        service.record_event(ActionEvent("evt-2", recommendation.recommendation_id, "u1", "M1", "completed", completed_event.occurred_at, {"activity": "改写内容"}))
    assert "情绪签到" in service.feedback_for(recommendation.recommendation_id)


def test_invalid_transition_expiry_and_decline_cooldown():
    repository = MemoryActionRepository()
    service = ActionService(repository, decline_cooldown=timedelta(hours=2))
    recommendation = service.recommend("u1", "s1", valid_action(), source_message_id="msg-1")
    with pytest.raises(ActionContractError):
        service.record_event(ActionEvent("evt", recommendation.recommendation_id, "u1", "M1", "completed", datetime.now(timezone.utc), {}))
    service.record_event(ActionEvent("evt-decline", recommendation.recommendation_id, "u1", "M1", "declined", datetime.now(timezone.utc), {"reason": "今天不方便"}))
    with pytest.raises(ActionContractError, match="cooldown"):
        service.recommend("u1", "s2", valid_action(), source_message_id="msg-2")
    with pytest.raises(ActionContractError, match="expired"):
        service.recommend("u2", "s1", valid_action("M2", expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()), source_message_id="msg-3")


def test_action_module_can_be_recommended_once_per_session():
    service = ActionService(MemoryActionRepository())
    first = service.recommend("u1", "s1", valid_action("M1"), source_message_id="msg-1")
    assert first.module == "M1"
    with pytest.raises(ActionAlreadyRecommended):
        service.recommend("u1", "s1", valid_action("M1"), source_message_id="msg-2")
    other_module = service.recommend("u1", "s1", valid_action("M2"), source_message_id="msg-3")
    assert other_module.module == "M2"
    other_session = service.recommend("u1", "s2", valid_action("M1"), source_message_id="msg-4")
    assert other_session.session_id == "s2"


def test_explicit_recommendation_can_repeat_in_same_session():
    service = ActionService(MemoryActionRepository())
    first = service.recommend("u1", "s1", valid_action("M1"), source_message_id="msg-1")
    second = service.recommend(
        "u1", "s1", valid_action("M1"),
        source_message_id="msg-2", deduplicate=False,
    )
    assert first.module == second.module == "M1"
    assert first.recommendation_id != second.recommendation_id


def test_completed_action_summary_requires_authorized_memory():
    memory = MemoryService(MemoryMemoryRepository())
    repository = MemoryActionRepository()
    service = ActionService(repository, memory_service=memory)
    recommendation = service.recommend("u1", "s1", valid_action("M3"), source_message_id="msg-1")
    service.record_event(ActionEvent("evt-accept-2", recommendation.recommendation_id, "u1", "M3", "accepted", datetime.now(timezone.utc), {}))
    service.record_event(ActionEvent("evt-complete", recommendation.recommendation_id, "u1", "M3", "completed", datetime.now(timezone.utc), {"activity": "积极练习", "effort": "写下了一个新的角度"}))
    assert memory.view("u1") == []
    memory.set_consent("u1", personalization=True)
    recommendation2 = service.recommend("u1", "s2", valid_action("M5"), source_message_id="msg-2")
    service.record_event(ActionEvent("evt-accept-3", recommendation2.recommendation_id, "u1", "M5", "accepted", datetime.now(timezone.utc), {}))
    service.record_event(ActionEvent("evt-complete-2", recommendation2.recommendation_id, "u1", "M5", "completed", datetime.now(timezone.utc), {"activity": "社区分享", "effort": "完成一次分享"}))
    assert "社区互动" in memory.get_context("u1")


def test_sensitive_action_summary_requires_sensitive_consent():
    memory = MemoryService(MemoryMemoryRepository())
    memory.set_consent("u1", personalization=True, sensitive=False)
    repository = MemoryActionRepository()
    service = ActionService(repository, memory_service=memory)

    first = service.recommend("u1", "s1", valid_action("M1"), source_message_id="m1")
    service.record_event(ActionEvent("m1-a", first.recommendation_id, "u1", "M1", "accepted", datetime.now(timezone.utc), {}))
    service.record_event(ActionEvent("m1-c", first.recommendation_id, "u1", "M1", "completed", datetime.now(timezone.utc), {"effort": "今天很难受"}))
    assert memory.list_current("u1") == []

    memory.set_consent("u1", personalization=True, sensitive=True)
    second = service.recommend("u1", "s2", valid_action("M3"), source_message_id="m2")
    service.record_event(ActionEvent("m3-a", second.recommendation_id, "u1", "M3", "accepted", datetime.now(timezone.utc), {}))
    service.record_event(ActionEvent("m3-c", second.recommendation_id, "u1", "M3", "completed", datetime.now(timezone.utc), {"effort": "做了私人练习"}))

    records = memory.list_current("u1")
    assert len(records) == 1
    assert records[0].sensitive is True
    assert "私人练习" not in records[0].content


def test_m5_free_text_is_fingerprinted_but_never_persisted():
    memory = MemoryService(MemoryMemoryRepository())
    memory.set_consent("u1", personalization=True)
    repository = MemoryActionRepository()
    service = ActionService(repository, memory_service=memory)
    recommendation = service.recommend("u1", "s1", valid_action("M5"), source_message_id="m5")
    service.record_event(ActionEvent("m5-a", recommendation.recommendation_id, "u1", "M5", "accepted", datetime.now(timezone.utc), {}))
    event = ActionEvent("m5-c", recommendation.recommendation_id, "u1", "M5", "completed", datetime.now(timezone.utc), {"effort": "这是不得落库的帖子正文"})

    service.record_event(event)

    stored = repository.get_event("m5-c")
    assert stored.metadata == {}
    assert "帖子正文" not in stored.summary
    assert stored.request_fingerprint.startswith("hmac-sha256:")
    assert "帖子正文" not in memory.get_context("u1")


def test_duplicate_replay_retries_failed_memory_bridge_idempotently():
    memory = MemoryService(MemoryMemoryRepository())
    memory.set_consent("u1", personalization=True)

    class FailOnceMemory:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def save_versioned_candidate(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary storage failure")
            return self.delegate.save_versioned_candidate(*args, **kwargs)

    repository = MemoryActionRepository()
    service = ActionService(repository, memory_service=FailOnceMemory(memory))
    recommendation = service.recommend("u1", "s1", valid_action("M5"), source_message_id="m5")
    service.record_event(ActionEvent("retry-a", recommendation.recommendation_id, "u1", "M5", "accepted", datetime.now(timezone.utc), {}))
    event = ActionEvent("retry-c", recommendation.recommendation_id, "u1", "M5", "completed", datetime.now(timezone.utc), {})

    with pytest.raises(ActionMemoryUnavailable):
        service.record_event_with_status(event)
    recommendation_after, created = service.record_event_with_status(event)

    assert recommendation_after.status == "completed"
    assert created is False
    assert len(memory.list_current("u1")) == 1


def test_expired_event_is_valid_but_late_accept_does_not_mutate_state():
    repository = MemoryActionRepository()
    service = ActionService(repository)
    now = datetime.now(timezone.utc)
    expired = ActionRecommendation(
        "expired-1", "u1", "s1", "M1", valid_action("M1"), "reason",
        "recommended", "source", now - timedelta(seconds=1), now - timedelta(minutes=1),
    )
    repository.add_recommendation(expired)

    result = service.record_event(ActionEvent(
        "expired-event", "expired-1", "u1", "M1", "expired", now, {},
    ))
    assert result.status == "expired"

    late = ActionRecommendation(
        "late-1", "u1", "s2", "M1", valid_action("M1"), "reason",
        "recommended", "source", now - timedelta(seconds=1), now - timedelta(minutes=1),
    )
    repository.add_recommendation(late)
    with pytest.raises(ActionContractError, match="expired"):
        service.record_event(ActionEvent(
            "late-event", "late-1", "u1", "M1", "accepted", now, {},
        ))
    assert repository.get("late-1").status == "recommended"

    early = ActionRecommendation(
        "early-1", "u1", "s3", "M1", valid_action("M1"), "reason",
        "recommended", "source", now + timedelta(minutes=5), now,
    )
    repository.add_recommendation(early)
    with pytest.raises(ActionContractError, match="not expired"):
        service.record_event(ActionEvent(
            "early-event", "early-1", "u1", "M1", "expired", now, {},
        ))


def test_legacy_and_rotated_fingerprints_upgrade_on_equivalent_replay():
    repository = MemoryActionRepository()
    old_service = ActionService(
        repository,
        reference_service=HmacReferenceService("old-" + "x" * 40, key_version="v1"),
    )
    recommendation = old_service.recommend(
        "u1", "s1", valid_action("M2"), source_message_id="rotate"
    )
    event = ActionEvent(
        "rotate-event", recommendation.recommendation_id, "u1", "M2",
        "accepted", datetime.now(timezone.utc), {},
    )
    old_service.record_event(event)
    stored = repository.get_event(event.event_id)
    repository._events[event.event_id] = ActionEvent(
        stored.event_id, stored.recommendation_id, stored.user_id, stored.module,
        stored.event_type, stored.occurred_at, stored.metadata, stored.summary,
        "legacy:" + stored.event_id,
    )

    _, legacy_created = old_service.record_event_with_status(event)
    assert legacy_created is False
    assert repository.get_event(event.event_id).request_fingerprint.startswith(
        "hmac-sha256:v1:"
    )

    new_service = ActionService(
        repository,
        reference_service=HmacReferenceService(
            "new-" + "y" * 40,
            key_version="v2",
            previous_keys={"v1": "old-" + "x" * 40},
        ),
    )
    _, rotated_created = new_service.record_event_with_status(event)
    assert rotated_created is False
    assert repository.get_event(event.event_id).request_fingerprint.startswith(
        "hmac-sha256:v2:"
    )


def test_rotated_event_replay_does_not_duplicate_action_memory():
    memory = MemoryService(MemoryMemoryRepository())
    memory.set_consent("u1", personalization=True)
    repository = MemoryActionRepository()
    old_service = ActionService(
        repository,
        memory_service=memory,
        reference_service=HmacReferenceService("old-" + "x" * 40, key_version="v1"),
    )
    recommendation = old_service.recommend(
        "u1", "s1", valid_action("M2"), source_message_id="memory-rotate"
    )
    old_service.record_event(ActionEvent(
        "memory-accept", recommendation.recommendation_id, "u1", "M2",
        "accepted", datetime.now(timezone.utc), {},
    ))
    completed = ActionEvent(
        "memory-complete", recommendation.recommendation_id, "u1", "M2",
        "completed", datetime.now(timezone.utc), {},
    )
    old_service.record_event(completed)

    new_service = ActionService(
        repository,
        memory_service=memory,
        reference_service=HmacReferenceService(
            "new-" + "y" * 40,
            key_version="v2",
            previous_keys={"v1": "old-" + "x" * 40},
        ),
    )
    new_service.record_event(completed)

    assert len(memory.list_current("u1")) == 1


def test_rotated_keyring_still_rejects_changed_metadata_replay():
    repository = MemoryActionRepository()
    old_secret = "old-" + "x" * 40
    old_service = ActionService(
        repository,
        reference_service=HmacReferenceService(old_secret, key_version="v1"),
    )
    recommendation = old_service.recommend(
        "u1", "s1", valid_action("M2"), source_message_id="rotation-conflict"
    )
    event = ActionEvent(
        "rotation-conflict-event", recommendation.recommendation_id, "u1", "M2",
        "accepted", datetime.now(timezone.utc), {"effort": "original"},
    )
    old_service.record_event(event)
    new_service = ActionService(
        repository,
        reference_service=HmacReferenceService(
            "new-" + "y" * 40,
            key_version="v2",
            previous_keys={"v1": old_secret},
        ),
    )

    with pytest.raises(ActionEventConflict):
        new_service.record_event(ActionEvent(
            event.event_id,
            event.recommendation_id,
            event.user_id,
            event.module,
            event.event_type,
            event.occurred_at,
            {"effort": "changed"},
        ))
