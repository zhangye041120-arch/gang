from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from API服务 import create_app
from tests.test_actions import valid_action
from tests.test_api_runtime_state import RecordingAgent
from xiaoliao_agent.actions import ActionEvent, ActionService, MemoryActionRepository
from xiaoliao_agent.memory import MemoryMemoryRepository, MemoryService
from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService


def make_client():
    memory = MemoryService(MemoryMemoryRepository())
    memory.set_consent("user-1", personalization=True, sensitive=True)
    actions = ActionService(MemoryActionRepository(), memory_service=memory)
    recommendation = actions.recommend(
        "user-1",
        "session-1",
        valid_action("M1"),
        source_message_id="source-1",
    )
    actions.record_event(ActionEvent(
        event_id="accepted-1",
        recommendation_id=recommendation.recommendation_id,
        user_id="user-1",
        module="M1",
        event_type="accepted",
        occurred_at=datetime.now(timezone.utc),
        metadata={},
    ))
    agent = RecordingAgent()
    agent.action_service = actions
    user_data = UserDataService(
        MemoryUserRepository(), memory_service=memory
    )
    user_data.get_or_create_user("user-1")
    user_data.set_consent("user-1", personalization=True, sensitive=True)
    app = create_app(
        lambda: agent,
        api_token="test-token",
        test_mode=True,
        user_data_service=user_data,
    )
    return TestClient(app), actions, memory, recommendation


def headers():
    return {"Authorization": "Bearer test-token"}


def event_payload(recommendation, **overrides):
    payload = {
        "event_id": "completed-1",
        "recommendation_id": recommendation.recommendation_id,
        "user_id": "user-1",
        "module": "M1",
        "event_type": "completed",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "summary": "完成情绪签到",
        "metadata": {"mood_score": 3},
    }
    payload.update(overrides)
    return payload


def test_action_event_first_write_and_identical_replay_report_duplicate_correctly():
    client, _, memory, recommendation = make_client()
    payload = event_payload(recommendation)
    with client:
        first = client.post("/v1/action-events", json=payload, headers=headers())
        second = client.post("/v1/action-events", json=payload, headers=headers())

    assert first.status_code == second.status_code == 200
    assert first.json() == {
        "status": "ok",
        "recommendation_id": recommendation.recommendation_id,
        "duplicate": False,
    }
    assert second.json()["duplicate"] is True
    assert len(memory.list_current("user-1")) == 1


def test_action_event_changed_replay_returns_conflict_without_second_memory():
    client, _, memory, recommendation = make_client()
    payload = event_payload(recommendation)
    with client:
        first = client.post("/v1/action-events", json=payload, headers=headers())
        changed = client.post(
            "/v1/action-events",
            json={**payload, "metadata": {"mood_score": 4}},
            headers=headers(),
        )

    assert first.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "AGENT_ACTION_EVENT_CONFLICT"
    assert len(memory.list_current("user-1")) == 1


def test_action_event_rejects_mismatched_subject_module_and_invalid_transition():
    client, _, _, recommendation = make_client()
    with client:
        wrong_user = client.post(
            "/v1/action-events",
            json=event_payload(recommendation, user_id="user-2"),
            headers=headers(),
        )
        wrong_module = client.post(
            "/v1/action-events",
            json=event_payload(
                recommendation, event_id="wrong-module", module="M2", metadata={}
            ),
            headers=headers(),
        )
        invalid_transition = client.post(
            "/v1/action-events",
            json=event_payload(
                recommendation,
                event_id="invalid-transition",
                event_type="accepted",
            ),
            headers=headers(),
        )

    assert wrong_user.status_code == 409
    assert wrong_module.status_code == 409
    assert invalid_transition.status_code == 409


def test_action_event_metadata_is_allowlisted_and_bounded():
    client, _, _, recommendation = make_client()
    with client:
        extra = client.post(
            "/v1/action-events",
            json=event_payload(
                recommendation,
                metadata={"mood_score": 3, "post_text": "不得持久化"},
            ),
            headers=headers(),
        )
        too_long = client.post(
            "/v1/action-events",
            json=event_payload(
                recommendation,
                event_id="too-long",
                metadata={"result_summary": "x" * 101},
            ),
            headers=headers(),
        )

    assert extra.status_code == 422
    assert too_long.status_code == 422


def test_action_event_occurred_at_requires_timezone():
    client, _, _, recommendation = make_client()
    payload = event_payload(
        recommendation,
        event_id="naive-time",
        occurred_at="2026-08-17T12:00:00",
    )

    with client:
        response = client.post("/v1/action-events", json=payload, headers=headers())

    assert response.status_code == 422


def test_m2_action_event_accepts_bounded_module_specific_metadata():
    client, actions, _, _ = make_client()
    recommendation = actions.recommend(
        "user-1", "session-m2", valid_action("M2"), source_message_id="m2-source"
    )
    actions.record_event(ActionEvent(
        "m2-accepted", recommendation.recommendation_id, "user-1", "M2",
        "accepted", datetime.now(timezone.utc), {},
    ))
    payload = event_payload(
        recommendation,
        event_id="m2-completed",
        module="M2",
        summary="完成一轮注意力训练",
        metadata={
            "training_type": "attention",
            "difficulty": "medium",
            "duration_seconds": 180,
            "result_summary": "completed",
        },
    )

    with client:
        response = client.post("/v1/action-events", json=payload, headers=headers())

    assert response.status_code == 200


def test_action_event_openapi_freezes_success_and_conflict_responses():
    client, _, _, _ = make_client()
    with client:
        operation = client.get("/openapi.json").json()["paths"][
            "/v1/action-events"
        ]["post"]

    success_content = operation["responses"]["200"]["content"]
    success_schema = next(iter(success_content.values()))["schema"]
    assert success_schema["$ref"].endswith("/ActionEventResponse")
    assert "409" in operation["responses"]
