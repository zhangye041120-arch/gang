from fastapi.testclient import TestClient

from API服务 import create_app
from tests.test_api_runtime_state import RecordingAgent
from xiaoliao_agent.memory import MemoryCandidate, MemoryMemoryRepository, MemoryService
from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService


def make_client():
    memory_service = MemoryService(MemoryMemoryRepository())
    agent = RecordingAgent()
    agent.memory_service = memory_service
    user_data = UserDataService(
        MemoryUserRepository(), memory_service=memory_service
    )
    user_data.get_or_create_user("user-1")
    user_data.get_or_create_user("user-2")
    user_data.set_consent("user-1", personalization=True)
    user_data.set_consent("user-2", personalization=True)
    record = memory_service.save_versioned_candidate(
        "user-1",
        MemoryCandidate(
            memory_type="profile",
            content="用户住在沈阳",
            confidence=1.0,
            source_message_id="seed-1",
            memory_key="profile.city",
            source_type="explicit",
            explicitly_stated=True,
        ),
    )
    app = create_app(
        lambda: agent,
        api_token="test-token",
        test_mode=True,
        user_data_service=user_data,
    )
    return TestClient(app), memory_service, record


def auth_headers():
    return {"Authorization": "Bearer test-token"}


def test_memory_api_lists_current_records_with_bounded_pagination():
    client, _, record = make_client()
    with client:
        response = client.get(
            "/v1/me/memories?user_id=user-1&limit=10&offset=0",
            headers=auth_headers(),
        )
        invalid = client.get(
            "/v1/me/memories?user_id=user-1&limit=101",
            headers=auth_headers(),
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["memory_id"] == record.memory_id
    assert response.json()["items"][0]["content"] == "用户住在沈阳"
    assert invalid.status_code == 422


def test_memory_api_correction_creates_new_version_and_denies_cross_user():
    client, memory_service, record = make_client()
    with client:
        denied = client.patch(
            f"/v1/me/memories/{record.memory_id}",
            json={"user_id": "user-2", "content": "越权修改"},
            headers=auth_headers(),
        )
        corrected = client.patch(
            f"/v1/me/memories/{record.memory_id}",
            json={"user_id": "user-1", "content": "用户现住大连"},
            headers=auth_headers(),
        )

    assert denied.status_code == 404
    assert corrected.status_code == 200
    assert corrected.json()["memory_id"] != record.memory_id
    assert corrected.json()["supersedes_memory_id"] == record.memory_id
    assert [item.content for item in memory_service.list_current("user-1")] == [
        "用户现住大连"
    ]


def test_memory_api_delete_is_user_scoped_and_immediately_removes_context():
    client, memory_service, record = make_client()
    assert memory_service.get_context("user-1")
    with client:
        denied = client.delete(
            f"/v1/me/memories/{record.memory_id}?user_id=user-2",
            headers=auth_headers(),
        )
        deleted = client.delete(
            f"/v1/me/memories/{record.memory_id}?user_id=user-1",
            headers=auth_headers(),
        )

    assert denied.status_code == 404
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert memory_service.get_context("user-1") == ""
