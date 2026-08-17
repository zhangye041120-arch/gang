from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService


def make_service():
    return UserDataService(MemoryUserRepository())


def test_user_creation_is_idempotent():
    service = make_service()
    first = service.get_or_create_user("user-1")
    second = service.get_or_create_user("user-1")
    assert first.user_id == second.user_id == "user-1"


def test_consent_grant_revoke_and_sync():
    service = make_service()
    service.get_or_create_user("user-1")
    service.set_consent("user-1", personalization=True, sensitive=True)
    assert service.consent_for("user-1") == (True, True)
    service.set_consent("user-1", personalization=False)
    assert service.consent_for("user-1") == (False, False)


def test_effective_consent_can_only_narrow_server_side_consent():
    service = make_service()
    service.get_or_create_user("user-1")

    assert service.effective_consent("user-1", True, True) == (False, False)
    service.set_consent("user-1", personalization=True, sensitive=True)
    assert service.effective_consent("user-1", True, True) == (True, True)
    assert service.effective_consent("user-1", True, False) == (True, False)
    assert service.effective_consent("user-1", False, True) == (False, False)


def test_conversation_pair_writes_two_events_and_summary():
    service = make_service()
    service.log_conversation_pair(
        "user-1",
        user_text="你好",
        reply="你好，我在听。",
        intent="chat",
        request_id="req-1",
    )
    events = service.repository.list_conversation_events("user-1")
    assert len(events) == 2
    assert {event.role for event in events} == {"user", "assistant"}
    assert all(event.content_ref.startswith("hmac-sha256:") for event in events)
    assert all(not event.content_ref.startswith("sha256:") for event in events)
    summary = service.summary("user-1")
    assert summary["user_id"] == "user-1"
    assert len(summary["recent_events"]) == 2


def test_delete_user_removes_events_and_consent():
    service = make_service()
    service.log_conversation_pair(
        "user-1",
        user_text="你好",
        reply="你好",
        intent="chat",
        request_id="req-1",
    )
    service.set_consent("user-1", personalization=True)
    service.delete_user("user-1")
    assert service.repository.list_conversation_events("user-1") == []
    assert service.consent_for("user-1") == (False, False)
