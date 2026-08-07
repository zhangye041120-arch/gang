from datetime import datetime, timedelta, timezone
import json

import pytest

from xiaoliao_agent.actions import (
    ActionAlreadyRecommended,
    ActionContractError,
    ActionEvent,
    ActionService,
    MemoryActionRepository,
)
from xiaoliao_agent.memory import MemoryCandidate, MemoryService
from xiaoliao_agent.memory_repository import MemoryMemoryRepository
from xiaoliao_agent.schemas import ActionPayload


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
    completed = service.record_event(ActionEvent("evt-2", recommendation.recommendation_id, "u1", "M1", "completed", datetime.now(timezone.utc), {"activity": "情绪签到", "effort": "完成了今天的记录"}))
    assert completed.status == "completed"
    duplicate = service.record_event(ActionEvent("evt-2", recommendation.recommendation_id, "u1", "M1", "completed", datetime.now(timezone.utc), {"activity": "改写内容"}))
    assert duplicate.status == "completed"
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
    assert "社区分享" in memory.get_context("u1")
