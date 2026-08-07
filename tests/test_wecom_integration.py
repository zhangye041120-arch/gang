from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from xiaoliao_agent.actions import ActionEvent, ActionService, MemoryActionRepository
from xiaoliao_agent.active_greeting import (
    GreetingPolicy,
    GreetingService,
    GreetingState,
    greeting_allowed,
    greeting_policy_from_settings,
)
from xiaoliao_agent.config import Settings
from xiaoliao_agent.crisis_referral import (
    CrisisReferralConfig,
    CrisisReferralRoute,
    CrisisReferralService,
    load_crisis_referral_config,
)
from xiaoliao_agent.memory import MemoryMemoryRepository, MemoryService
from xiaoliao_agent.notifications import CrisisNotifier
from xiaoliao_agent.wecom import (
    MessageDedupStore,
    MessageOrderStore,
    WeComChatAdapter,
    WeComCryptoError,
    WeComSignatureError,
    decrypt_callback,
    verify_callback_signature,
)
from xiaoliao_agent.action_events import UnknownActionModuleError, handle_action_event


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def event_payload(**overrides):
    data = {
        "event_id": "msg-001",
        "user_id": "user-001",
        "session_id": "session-001",
        "sequence": 1,
        "message": "你好",
        "context": {"consent": {"personalization": False}, "user_summary": ""},
    }
    data.update(overrides)
    return data


def test_callback_signature_verification_accepts_only_correct_signature():
    token = "wecom-token"
    timestamp = "1700000000"
    nonce = "nonce-1"
    echostr = "echo-1"
    expected = hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, echostr])).encode("utf-8")
    ).hexdigest()
    now = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    assert verify_callback_signature(token, timestamp, nonce, echostr, expected, now=now) is True
    assert verify_callback_signature(token, timestamp, nonce, echostr, "bad-signature", now=now) is False


def test_callback_signature_rejects_stale_timestamp():
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
    stale = int((now - timedelta(minutes=10)).timestamp())
    with pytest.raises(WeComSignatureError):
        verify_callback_signature(
            "token", str(stale), "nonce", "echo", "ignored", now=now
        )


def test_callback_decrypt_failure_raises_stable_error():
    def broken_decrypt(_payload):
        raise RuntimeError("crypto backend failed")

    with pytest.raises(WeComCryptoError):
        decrypt_callback("encrypted", broken_decrypt)


def test_duplicate_message_event_is_processed_once():
    calls = []

    def chat_client(**kwargs):
        calls.append(kwargs)
        return {"reply": "你好", "intent": "chat", "blocked": False}

    adapter = WeComChatAdapter(
        chat_client=chat_client,
        dedup=MessageDedupStore(retention_seconds=3600),
        order=MessageOrderStore(),
    )
    first = adapter.handle_message(event_payload())
    second = adapter.handle_message(event_payload())
    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert len(calls) == 1


def test_out_of_order_message_sequence_is_rejected():
    calls = []

    def chat_client(**kwargs):
        calls.append(kwargs)
        return {"reply": "ok", "intent": "chat", "blocked": False}

    adapter = WeComChatAdapter(
        chat_client=chat_client,
        dedup=MessageDedupStore(),
        order=MessageOrderStore(),
    )
    adapter.handle_message(event_payload(sequence=2))
    result = adapter.handle_message(event_payload(sequence=1, event_id="msg-old"))
    assert result["status"] == "out_of_order"
    assert len(calls) == 1


def test_chat_timeout_returns_stable_failure_without_marking_duplicate():
    def chat_client(**kwargs):
        raise TimeoutError("model timeout")

    adapter = WeComChatAdapter(
        chat_client=chat_client,
        dedup=MessageDedupStore(),
        order=MessageOrderStore(),
    )
    result = adapter.handle_message(event_payload(event_id="msg-timeout"))
    assert result["status"] == "chat_failed"
    assert result["error_code"] == "EVAL_TIMEOUT"
    assert adapter.dedup.contains("msg-timeout") is False


def test_adapter_uses_event_id_as_idempotency_key_and_never_sends():
    captured = {}

    def chat_client(**kwargs):
        captured.update(kwargs)
        return {"reply": "ok", "intent": "chat", "blocked": False}

    adapter = WeComChatAdapter(
        chat_client=chat_client,
        dedup=MessageDedupStore(),
        order=MessageOrderStore(),
    )
    result = adapter.handle_message(event_payload(event_id="msg-idem"))
    assert captured["idempotency_key"] == "msg-idem"
    assert result["reply"] == "ok"
    assert "sender" not in vars(adapter)


def test_active_greeting_is_disabled_by_default():
    policy = GreetingPolicy(enabled=False)
    state = GreetingState(user_id="u", personalization=True, active_message_authorized=True)
    allowed, reason = greeting_allowed(policy, state, now=datetime.now(timezone.utc))
    assert allowed is False
    assert reason == "global_disabled"


def test_active_greeting_settings_default_off_and_referral_example_loads():
    settings = Settings()
    assert settings.wecom_active_greeting_enabled is False
    assert greeting_policy_from_settings(settings).enabled is False
    config = load_crisis_referral_config(PROJECT_ROOT / "crisis_referral_config.example.json")
    assert config.routes["华东"].hotline is None
    assert config.routes["华东"].on_call is None


def test_active_greeting_respects_user_disable_and_dnd():
    now = datetime(2026, 8, 7, 23, 0, 0, tzinfo=timezone.utc)
    policy = GreetingPolicy(enabled=True, dnd_periods=((22, 6),))
    state = GreetingState(
        user_id="u",
        personalization=True,
        active_message_authorized=True,
        user_disabled=True,
    )
    assert greeting_allowed(policy, state, now=now) == (False, "user_disabled")
    state = GreetingState(
        user_id="u",
        personalization=True,
        active_message_authorized=True,
        user_disabled=False,
    )
    assert greeting_allowed(policy, state, now=now) == (False, "dnd")


def test_active_greeting_respects_recent_activity_and_frequency_cap():
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    policy = GreetingPolicy(enabled=True, recent_active_hours=24, max_per_day=1)
    recent = GreetingState(
        user_id="u",
        personalization=True,
        active_message_authorized=True,
        last_active_at=now - timedelta(hours=1),
    )
    assert greeting_allowed(policy, recent, now=now) == (False, "recent_active")
    capped = GreetingState(
        user_id="u",
        personalization=True,
        active_message_authorized=True,
        sent_today=1,
    )
    assert greeting_allowed(policy, capped, now=now) == (False, "daily_limit")


def test_withdrawing_consent_blocks_greeting_and_memory_use():
    repository = MemoryMemoryRepository()
    memory = MemoryService(repository)
    memory.set_consent("user-x", personalization=True)
    policy = GreetingPolicy(enabled=True)
    state = GreetingState(
        user_id="user-x",
        personalization=True,
        active_message_authorized=True,
    )
    assert greeting_allowed(policy, state, now=datetime.now(timezone.utc))[0] is True
    memory.set_consent("user-x", personalization=False)
    state = GreetingState(
        user_id="user-x",
        personalization=False,
        active_message_authorized=False,
    )
    assert greeting_allowed(policy, state, now=datetime.now(timezone.utc)) == (False, "personalization_required")
    assert memory.get_context("user-x") == ""


def test_greeting_service_prevents_duplicate_send():
    policy = GreetingPolicy(enabled=True)
    service = GreetingService(policy=policy)
    now = datetime(2026, 8, 7, 9, 0, 0, tzinfo=timezone.utc)
    first = service.try_send("user-y", now=now)
    second = service.try_send("user-y", now=now)
    assert first["status"] == "ok"
    assert second["status"] == "duplicate"


def valid_action(module="M1"):
    return {
        "type": "miniprogram",
        "module": module,
        "page": {
            "M1": "/pages/checkin/index",
            "M2": "/pages/games/index",
            "M3": "/pages/exercise/index",
            "M5": "/pages/community/index",
        }[module],
        "params": {},
        "reason": "wecom_completion",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def test_action_event_mapping_accepts_four_modules_and_rejects_unknown():
    repository = MemoryActionRepository()
    service = ActionService(repository)
    recommendation = service.recommend("u", "s", valid_action("M1"), source_message_id="src-1")
    service.record_event(ActionEvent(
        event_id="accept-1",
        recommendation_id=recommendation.recommendation_id,
        user_id="u",
        module="M1",
        event_type="accepted",
        occurred_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        metadata={},
    ))
    result = handle_action_event(service, {
        "event_id": "event-1",
        "recommendation_id": recommendation.recommendation_id,
        "user_id": "u",
        "module": "M1",
        "event_type": "completed",
        "occurred_at": "2099-01-01T00:00:00+00:00",
        "metadata": {"activity": "签到", "effort": "完成了一次记录"},
    })
    assert result["status"] == "ok"
    with pytest.raises(UnknownActionModuleError):
        handle_action_event(service, {
            "event_id": "event-9",
            "recommendation_id": "x",
            "user_id": "u",
            "module": "M9",
            "event_type": "completed",
            "occurred_at": "2099-01-01T00:00:00+00:00",
            "metadata": {},
        })


def test_action_event_duplicate_is_idempotent_without_new_memory():
    memory_repository = MemoryMemoryRepository()
    memory = MemoryService(memory_repository)
    memory.set_consent("u", personalization=True)
    repository = MemoryActionRepository()
    service = ActionService(repository, memory_service=memory)
    recommendation = service.recommend("u", "s", valid_action("M1"), source_message_id="src-2")
    service.record_event(ActionEvent(
        event_id="accept-2",
        recommendation_id=recommendation.recommendation_id,
        user_id="u",
        module="M1",
        event_type="accepted",
        occurred_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        metadata={},
    ))
    payload = {
        "event_id": "event-dup",
        "recommendation_id": recommendation.recommendation_id,
        "user_id": "u",
        "module": "M1",
        "event_type": "completed",
        "occurred_at": "2099-01-01T00:00:00+00:00",
        "metadata": {"activity": "签到", "effort": "完成了一次记录"},
    }
    first = handle_action_event(service, payload)
    second = handle_action_event(service, payload)
    assert first["status"] == second["status"] == "ok"
    assert len(memory.view("u")) == 1


def test_crisis_referral_missing_contact_records_alert_without_fabrication():
    config = CrisisReferralConfig(routes={
        "华东": CrisisReferralRoute(
            region="华东",
            hotline=None,
            on_call=None,
            authorization_required=True,
        ),
    })
    service = CrisisReferralService(config=config)
    result = service.refer({
        "event_id": "crisis-1",
        "user_id": "u",
        "region": "华东",
        "authorized": True,
    })
    assert result["status"] == "alert"
    assert "crisis_contact_missing" in result["alerts"]
    assert "138" not in json.dumps(result, ensure_ascii=False)


def test_crisis_referral_failure_is_idempotent_for_same_event():
    attempts = []

    def failing_sender(event):
        attempts.append(event.event_id)
        raise RuntimeError("referral unavailable")

    config = CrisisReferralConfig(routes={
        "华东": CrisisReferralRoute(
            region="华东",
            hotline="hotline-config-placeholder",
            on_call="oncall-config-placeholder",
            authorization_required=True,
        ),
    })
    notifier = CrisisNotifier(route="华东", sender=failing_sender, max_attempts=1)
    service = CrisisReferralService(config=config, notifier=notifier)
    payload = {
        "event_id": "crisis-dup",
        "user_id": "u",
        "region": "华东",
        "authorized": True,
    }
    first = service.refer(payload)
    second = service.refer(payload)
    assert first["alerts"] == ["notification_failed"]
    assert second["alerts"] == []
    assert attempts == ["crisis-dup"]


def test_wecom_docs_cover_checklist_drill_and_rollback():
    checklist = (PROJECT_ROOT / "docs" / "企微联调检查单.md").read_text(encoding="utf-8")
    drill = (PROJECT_ROOT / "docs" / "危机演练记录.md").read_text(encoding="utf-8")
    rollback = (PROJECT_ROOT / "docs" / "上线回滚方案.md").read_text(encoding="utf-8")
    assert "回调" in checklist and "发送权限" in checklist
    assert "未执行" in drill
    assert "回滚触发条件" in rollback and "负责人" in rollback
