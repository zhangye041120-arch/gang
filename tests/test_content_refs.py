import pytest

from xiaoliao_agent.content_refs import HmacReferenceService


def test_reference_domains_are_separated_and_versioned():
    service = HmacReferenceService("s" * 48, key_version="2026-08")

    subject = service.subject_hmac("user-1")
    conversation = service.conversation_ref("你好")
    candidate = service.candidate_reply_ref("你好")

    assert subject.startswith("hmac-sha256:2026-08:")
    assert len({subject, conversation, candidate}) == 3
    assert "user-1" not in subject
    assert "你好" not in conversation


def test_fingerprint_is_stable_within_purpose_and_differs_across_purposes():
    service = HmacReferenceService("s" * 48)
    canonical = b'{"event":"completed"}'

    first = service.fingerprint("action-event", canonical)
    second = service.fingerprint("action-event", canonical)
    other = service.fingerprint("gateway-request", canonical)

    assert first == second
    assert first != other


def test_secret_is_not_exposed_by_repr_or_errors():
    secret = "not-visible-" + "x" * 40
    service = HmacReferenceService(secret)

    assert secret not in repr(service)
    with pytest.raises(ValueError) as exc_info:
        service.fingerprint("", b"data")
    assert secret not in str(exc_info.value)


def test_subject_hmac_candidates_include_current_then_previous_versions():
    service = HmacReferenceService(
        "c" * 48,
        key_version="v3",
        previous_keys={"v2": "b" * 48, "v1": "a" * 48},
    )

    assert service.subject_hmac_candidates("user-1") == (
        "hmac-sha256:v3:ad821b1535155f02c55315c376d40c6861d59ead4717f53821ea437012e24d04",
        "hmac-sha256:v2:afc645e64b2d5792d50c749df495257a399ec4a4791d6184a3ab9cbd6bc484e5",
        "hmac-sha256:v1:4aae3924c5a4e731be505e77e925734f1459c2197212edaf674a96e20c3375d1",
    )


def test_previous_secrets_are_not_exposed_by_repr_or_errors():
    current_secret = "current-not-visible-" + "x" * 32
    previous_secret = "previous-not-visible-" + "y" * 32
    invalid_secret = "invalid-not-visible-" + "z" * 32
    service = HmacReferenceService(
        current_secret,
        key_version="v2",
        previous_keys={"v1": previous_secret},
    )

    assert current_secret not in repr(service)
    assert previous_secret not in repr(service)
    with pytest.raises(ValueError) as exc_info:
        HmacReferenceService(
            current_secret,
            key_version="v2",
            previous_keys={"v1": previous_secret, "bad version": invalid_secret},
        )
    assert current_secret not in str(exc_info.value)
    assert previous_secret not in str(exc_info.value)
    assert invalid_secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "previous_keys",
    [
        {"v2": "p" * 48},
        {"v1": "c" * 48},
        {"v1": "p" * 48, "legacy": "p" * 48},
    ],
)
def test_reference_service_rejects_conflicting_keyring(previous_keys):
    with pytest.raises(ValueError):
        HmacReferenceService(
            "c" * 48,
            key_version="v2",
            previous_keys=previous_keys,
        )


def test_agent_injects_the_same_reference_service_into_actions():
    from tests.test_agent_pipeline import FakeInspectorClient, FakeMainClient
    from xiaoliao_agent.agent import XiaoliaoAgent
    from xiaoliao_agent.config import Settings

    agent = XiaoliaoAgent(
        Settings(privacy_hmac_secret="p" * 48),
        main_client=FakeMainClient(),
        inspector_client=FakeInspectorClient(),
    )

    assert agent.action_service.reference_service is agent.reference_service


@pytest.mark.parametrize("secret,key_version", [("", "v1"), ("secret", "bad version")])
def test_invalid_reference_configuration_is_rejected(secret, key_version):
    with pytest.raises(ValueError):
        HmacReferenceService(secret, key_version=key_version)
