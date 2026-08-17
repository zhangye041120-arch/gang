import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
import os
import uuid

import pytest

from tests.test_actions import valid_action
from xiaoliao_agent.actions import ActionService, MemoryActionRepository
from xiaoliao_agent.actions import PostgresActionRepository
from xiaoliao_agent.content_refs import HmacReferenceService
from xiaoliao_agent.crisis import MemoryCrisisEventRepository, new_crisis_event
from xiaoliao_agent.crisis import PostgresCrisisEventRepository
from xiaoliao_agent.memory import (
    MemoryCandidate,
    MemoryMemoryRepository,
    MemoryService,
    PostgresMemoryRepository,
)
from xiaoliao_agent.privacy import (
    PrivacyDeletionService,
    SubjectDeletedError,
    SubjectPrivacyGuard,
)
from xiaoliao_agent.quality import InspectionLog, MemoryQualityRepository, QualityService
from xiaoliao_agent.quality import PostgresQualityRepository
from xiaoliao_agent.reminders import MemoryReminderRepository, Reminder, ReminderService
from xiaoliao_agent.reminders import PostgresReminderRepository
from xiaoliao_agent.user_data import MemoryUserRepository, UserDataService
from xiaoliao_agent.user_data import PostgresUserRepository


USER_ID = "privacy-user"
PRIVACY_DATABASE_URL = os.getenv("PRIVACY_TEST_DATABASE_URL", "")


class RuntimeState:
    def __init__(self, *, fail_delete=False):
        self.fail_delete = fail_delete
        self.tombstoned = set()
        self.deleted = []

    async def begin_deletion(self, subject_hmac, ttl_seconds=2_592_000):
        self.tombstoned.add(subject_hmac)
        return True

    async def delete_subject(self, subject_hmac):
        if self.fail_delete:
            raise RuntimeError("redis unavailable")
        self.deleted.append(subject_hmac)
        return 3


def inspection(subject_hmac):
    return InspectionLog(
        request_id="quality-request",
        message_id="quality-message",
        user_hash=subject_hmac,
        candidate_reply_ref="hmac-sha256:v1:" + "a" * 64,
        crisis_detected=False,
        safety_violation=False,
        intent_accurate=True,
        age_appropriate=True,
        cbt_appropriate=True,
        issues=[],
        latency_ms=1,
        main_model="main",
        inspector_model="inspector",
        prompt_version="1.0.0",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost=None,
        error_pattern="none",
        lesson_ref=None,
        subject_hmac=subject_hmac,
        subject_id=USER_ID,
    )


def seeded_services(*, fail_redis=False):
    references = HmacReferenceService("privacy-" + "p" * 40)
    guard = SubjectPrivacyGuard(references)
    user_repository = MemoryUserRepository(privacy_guard=guard)
    memory_repository = MemoryMemoryRepository(privacy_guard=guard)
    action_repository = MemoryActionRepository(privacy_guard=guard)
    reminder_repository = MemoryReminderRepository(privacy_guard=guard)
    crisis_repository = MemoryCrisisEventRepository(privacy_guard=guard)
    quality_repository = MemoryQualityRepository(privacy_guard=guard)
    memory = MemoryService(memory_repository)
    users = UserDataService(user_repository, memory_service=memory, reference_service=references)
    actions = ActionService(action_repository, memory_service=memory, reference_service=references)
    reminders = ReminderService(reminder_repository)
    quality = QualityService(quality_repository)

    users.get_or_create_user(USER_ID)
    users.set_consent(USER_ID, personalization=True, sensitive=True)
    users.log_conversation_pair(
        USER_ID,
        user_text="private user text",
        reply="private assistant text",
        intent="chat",
        request_id="conversation-request",
    )
    memory.save_versioned_candidate(
        USER_ID,
        MemoryCandidate(
            memory_type="profile",
            content="private memory",
            confidence=1.0,
            source_message_id="memory-source",
            memory_key="profile.private",
            explicitly_stated=True,
        ),
    )
    actions.recommend(
        USER_ID, "session", valid_action("M2"), source_message_id="action-source"
    )
    reminder_repository.add(Reminder(
        "reminder-1",
        USER_ID,
        "private reminder",
        datetime.now(timezone.utc) + timedelta(hours=1),
        None,
        datetime.now(timezone.utc),
        "reminder-fingerprint",
    ))
    crisis_repository.add(new_crisis_event(
        user_id=USER_ID,
        session_id="session",
        evidence_code="I001",
        route="on-call",
        prompt_version="1.0.0",
        response_version="1.0.0",
    ))
    quality.write_log(inspection(references.subject_hmac(USER_ID)))
    runtime = RuntimeState(fail_delete=fail_redis)
    deletion = PrivacyDeletionService(
        reference_service=references,
        privacy_guard=guard,
        user_data_service=users,
        memory_service=memory,
        action_service=actions,
        reminder_service=reminders,
        crisis_repository=crisis_repository,
        quality_service=quality,
        runtime_state=runtime,
    )
    return {
        "references": references,
        "guard": guard,
        "users": users,
        "memory": memory,
        "actions": actions,
        "reminders": reminder_repository,
        "crisis": crisis_repository,
        "quality": quality,
        "runtime": runtime,
        "deletion": deletion,
    }


def test_complete_deletion_removes_all_identifiable_state_and_keeps_hmac_tombstone():
    state = seeded_services()
    subject_hmac = state["references"].subject_hmac(USER_ID)

    report = asyncio.run(
        state["deletion"].delete_user(USER_ID, subject_hmac, "delete-request-1")
    )

    assert report.status == "completed"
    assert report.subject_hmac == subject_hmac
    assert report.redis_cleaned is True
    assert USER_ID not in repr(report)
    assert state["users"].repository.list_conversation_events(USER_ID) == []
    assert state["users"].consent_for(USER_ID) == (False, False)
    assert state["memory"].repository.list_for_user(USER_ID, include_deleted=True) == []
    assert state["actions"].repository.list_for_user(USER_ID) == []
    assert state["reminders"].list_for_user(USER_ID) == []
    assert all(event.user_id != USER_ID for event in state["crisis"].list_events())
    assert state["quality"].repository.logs == {}
    assert state["guard"].is_tombstoned(USER_ID)


def test_redis_cleanup_failure_is_visible_and_retryable_without_raw_user_id():
    state = seeded_services(fail_redis=True)
    subject_hmac = state["references"].subject_hmac(USER_ID)

    report = asyncio.run(
        state["deletion"].delete_user(USER_ID, subject_hmac, "delete-request-2")
    )
    assert report.status == "redis_pending"
    assert report.redis_cleaned is False
    assert USER_ID not in repr(state["deletion"].pending_audits())

    state["runtime"].fail_delete = False
    assert asyncio.run(state["deletion"].retry_pending()) == 1
    assert state["deletion"].pending_audits() == []


def test_tombstoned_subject_cannot_recreate_user_memory_or_action():
    state = seeded_services()
    subject_hmac = state["references"].subject_hmac(USER_ID)
    asyncio.run(
        state["deletion"].delete_user(USER_ID, subject_hmac, "delete-request-3")
    )

    with pytest.raises(SubjectDeletedError):
        state["users"].get_or_create_user(USER_ID)
    with pytest.raises(SubjectDeletedError):
        state["memory"].save_versioned_candidate(
            USER_ID,
            MemoryCandidate(
                memory_type="profile",
                content="must not return",
                confidence=1.0,
                source_message_id="after-delete",
                memory_key="profile.after-delete",
                explicitly_stated=True,
            ),
        )
    with pytest.raises(SubjectDeletedError):
        state["actions"].recommend(
            USER_ID, "new-session", valid_action("M2"), source_message_id="after-delete"
        )
    alerts = state["quality"].write_log(inspection(subject_hmac))
    assert alerts == ["inspection_log_subject_deleted"]
    assert state["quality"].retry_queue == []


def test_deletion_race_cannot_leave_memory_or_action_after_tombstone():
    state = seeded_services()
    subject_hmac = state["references"].subject_hmac(USER_ID)
    barrier = Barrier(3)

    def delete():
        barrier.wait()
        return asyncio.run(state["deletion"].delete_user(
            USER_ID, subject_hmac, "delete-race"
        ))

    def save_memory():
        barrier.wait()
        try:
            state["memory"].save_versioned_candidate(
                USER_ID,
                MemoryCandidate(
                    memory_type="profile",
                    content="racing memory",
                    confidence=1.0,
                    source_message_id="race-memory",
                    memory_key="profile.race",
                    explicitly_stated=True,
                ),
            )
        except SubjectDeletedError:
            return "deleted"
        return "written"

    def save_action():
        barrier.wait()
        try:
            state["actions"].recommend(
                USER_ID,
                "race-session",
                valid_action("M2"),
                source_message_id="race-action",
                deduplicate=False,
            )
        except SubjectDeletedError:
            return "deleted"
        return "written"

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(delete),
            executor.submit(save_memory),
            executor.submit(save_action),
        ]
        results = [future.result() for future in futures]

    assert results[0].status == "completed"
    assert state["guard"].is_tombstoned(USER_ID)
    assert state["memory"].repository.list_for_user(USER_ID, include_deleted=True) == []
    assert state["actions"].repository.list_for_user(USER_ID) == []


@pytest.mark.skipif(
    not PRIVACY_DATABASE_URL,
    reason="PRIVACY_TEST_DATABASE_URL is not configured",
)
def test_postgres_deletion_is_complete_and_tombstone_blocks_recreation():
    psycopg = pytest.importorskip("psycopg")
    user_id = "privacy-db-" + uuid.uuid4().hex
    references = HmacReferenceService("privacy-db-" + "p" * 40)
    guard = SubjectPrivacyGuard(references)
    memory = MemoryService(PostgresMemoryRepository(
        PRIVACY_DATABASE_URL, privacy_guard=guard
    ), cache_enabled=False)
    users = UserDataService(
        PostgresUserRepository(PRIVACY_DATABASE_URL, privacy_guard=guard),
        memory_service=memory,
        reference_service=references,
    )
    actions = ActionService(
        PostgresActionRepository(PRIVACY_DATABASE_URL, privacy_guard=guard),
        memory_service=memory,
        reference_service=references,
    )
    reminders = ReminderService(PostgresReminderRepository(
        PRIVACY_DATABASE_URL, privacy_guard=guard
    ))
    crisis = PostgresCrisisEventRepository(
        PRIVACY_DATABASE_URL, privacy_guard=guard
    )
    quality = QualityService(PostgresQualityRepository(
        PRIVACY_DATABASE_URL, privacy_guard=guard
    ))
    subject_hmac = references.subject_hmac(user_id)

    users.get_or_create_user(user_id)
    users.set_consent(user_id, personalization=True, sensitive=True)
    users.log_conversation_pair(
        user_id,
        user_text="database private text",
        reply="database private reply",
        intent="chat",
        request_id="conversation-" + uuid.uuid4().hex,
    )
    memory.save_versioned_candidate(user_id, MemoryCandidate(
        memory_type="profile",
        content="database private memory",
        confidence=1.0,
        source_message_id="db-memory",
        memory_key="profile.database",
        explicitly_stated=True,
    ))
    actions.recommend(
        user_id, "db-session", valid_action("M2"), source_message_id="db-action"
    )
    reminder = Reminder(
        "rem-" + uuid.uuid4().hex,
        user_id,
        "database private reminder",
        datetime.now(timezone.utc) + timedelta(hours=1),
        None,
        datetime.now(timezone.utc),
        uuid.uuid4().hex,
    )
    reminders.repository.create_if_absent(
        reminder, since=datetime.now(timezone.utc) - timedelta(hours=24)
    )
    crisis.add(new_crisis_event(
        user_id=user_id,
        session_id="db-session",
        evidence_code="I001",
        route="on-call",
        prompt_version="1.0.0",
        response_version="1.0.0",
    ))
    lesson = quality.repository.add_lesson(
        "database private lesson",
        "none",
        "1.0.0",
        subject_hmac=subject_hmac,
        request_id="quality-" + uuid.uuid4().hex,
    )
    quality.write_log(inspection(subject_hmac).__class__(
        **{
            **inspection(subject_hmac).__dict__,
            "request_id": "quality-" + uuid.uuid4().hex,
            "lesson_ref": lesson.lesson_id,
            "subject_id": user_id,
        }
    ))
    deletion = PrivacyDeletionService(
        reference_service=references,
        privacy_guard=guard,
        user_data_service=users,
        memory_service=memory,
        action_service=actions,
        reminder_service=reminders,
        crisis_repository=crisis,
        quality_service=quality,
        runtime_state=None,
        database_url=PRIVACY_DATABASE_URL,
        legacy_quality_salt="legacy-quality",
    )

    report = asyncio.run(deletion.delete_user(
        user_id, subject_hmac, "delete-db-" + uuid.uuid4().hex
    ))

    assert report.status == "completed"
    with psycopg.connect(PRIVACY_DATABASE_URL) as connection:
        for table in (
            "ai_users", "ai_consents", "ai_conversation_events", "ai_memories",
            "ai_action_recommendations", "ai_action_events", "ai_reminders",
            "ai_crisis_events",
        ):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE user_id=%s", (user_id,)
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM ai_inspection_logs WHERE subject_hmac=%s",
            (subject_hmac,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM ai_lessons WHERE lesson_id=%s", (lesson.lesson_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM ai_privacy_tombstones WHERE subject_hmac=%s",
            (subject_hmac,),
        ).fetchone()[0] == "deleted"

    with pytest.raises(SubjectDeletedError):
        users.get_or_create_user(user_id)

    rotated_references = HmacReferenceService(
        "privacy-db-new-" + "n" * 40,
        key_version="v2",
        previous_keys={"v1": "privacy-db-" + "p" * 40},
    )
    rotated_guard = SubjectPrivacyGuard(rotated_references)
    rotated_users = UserDataService(
        PostgresUserRepository(
            PRIVACY_DATABASE_URL, privacy_guard=rotated_guard
        ),
        reference_service=rotated_references,
    )
    with pytest.raises(SubjectDeletedError):
        rotated_users.get_or_create_user(user_id)


@pytest.mark.skipif(
    not PRIVACY_DATABASE_URL,
    reason="PRIVACY_TEST_DATABASE_URL is not configured",
)
def test_postgres_redis_pending_audit_retries_after_service_restart():
    psycopg = pytest.importorskip("psycopg")
    user_id = "privacy-retry-" + uuid.uuid4().hex
    request_id = "delete-retry-" + uuid.uuid4().hex
    references = HmacReferenceService("privacy-retry-" + "r" * 40)
    guard = SubjectPrivacyGuard(references)
    memory = MemoryService(PostgresMemoryRepository(
        PRIVACY_DATABASE_URL, privacy_guard=guard
    ), cache_enabled=False)
    users = UserDataService(
        PostgresUserRepository(PRIVACY_DATABASE_URL, privacy_guard=guard),
        memory_service=memory,
        reference_service=references,
    )
    users.get_or_create_user(user_id)
    failing_runtime = RuntimeState(fail_delete=True)
    first = PrivacyDeletionService(
        reference_service=references,
        privacy_guard=guard,
        user_data_service=users,
        memory_service=memory,
        action_service=None,
        reminder_service=None,
        crisis_repository=None,
        quality_service=None,
        runtime_state=failing_runtime,
        database_url=PRIVACY_DATABASE_URL,
        legacy_quality_salt="legacy-quality",
    )

    report = asyncio.run(first.delete_user(
        user_id, references.subject_hmac(user_id), request_id
    ))
    assert report.status == "redis_pending"

    healthy_runtime = RuntimeState()
    restarted = PrivacyDeletionService(
        reference_service=references,
        privacy_guard=SubjectPrivacyGuard(references),
        user_data_service=users,
        memory_service=memory,
        action_service=None,
        reminder_service=None,
        crisis_repository=None,
        quality_service=None,
        runtime_state=healthy_runtime,
        database_url=PRIVACY_DATABASE_URL,
        legacy_quality_salt="legacy-quality",
    )
    assert asyncio.run(restarted.retry_pending()) >= 1
    with psycopg.connect(PRIVACY_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT status FROM ai_privacy_deletion_audits WHERE request_id=%s",
            (request_id,),
        ).fetchone()[0] == "completed"
