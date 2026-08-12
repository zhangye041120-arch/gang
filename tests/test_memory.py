from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from xiaoliao_agent.memory import (
    ConsentRequiredError,
    MemoryCandidate,
    MemoryService,
    SensitiveConsentRequiredError,
)
from xiaoliao_agent.memory_repository import MemoryMemoryRepository, MemoryVectorIndex
from xiaoliao_agent.agent import XiaoliaoAgent
from xiaoliao_agent.config import Settings
import json


def candidate(content="用户喜欢听戏曲", **overrides):
    data = {
        "memory_type": "interest_preference",
        "content": content,
        "confidence": 0.95,
        "source_message_id": "msg-001",
        "consent_scope": "personalization",
        "explicitly_stated": True,
        "sensitive": False,
    }
    data.update(overrides)
    return MemoryCandidate(**data)


def make_service():
    repository = MemoryMemoryRepository()
    vectors = MemoryVectorIndex()
    return MemoryService(repository, vector_index=vectors), repository, vectors


class PassingInspector:
    def chat(self, messages, *, json_mode=False):
        return json.dumps({
            "crisis_detected": False,
            "safety_violation": False,
            "intent_accurate": True,
            "age_appropriate": True,
            "cbt_appropriate": True,
            "issues": [],
            "suggestion": "",
            "error_pattern": "none",
            "lesson": "",
        }, ensure_ascii=False)


class EchoMainClient:
    def __init__(self):
        self.messages = None

    def chat(self, messages, *, json_mode=False):
        self.messages = messages
        return json.dumps({
            "reply": "好，我记住了。",
            "intent": "chat",
            "action": None,
            "risk_hint": "none",
        }, ensure_ascii=False)


class InvalidMainClient:
    def chat(self, messages, *, json_mode=False):
        return "这不是结构化 JSON"


def test_default_without_consent_neither_writes_nor_reads():
    service, repository, _ = make_service()
    with pytest.raises(ConsentRequiredError):
        service.save_candidate("user-1", candidate())
    assert service.get_context("user-1") == ""
    assert repository.list_for_user("user-1") == []


def test_authorized_write_deduplicates_identical_content():
    service, repository, _ = make_service()
    service.set_consent("user-1", personalization=True)
    first = service.save_candidate("user-1", candidate())
    second = service.save_candidate("user-1", candidate(source_message_id="msg-002"))
    assert first.memory_id == second.memory_id
    assert len(repository.list_for_user("user-1")) == 1


def test_same_source_updates_existing_memory():
    service, repository, _ = make_service()
    service.set_consent("user-1", personalization=True)
    original = service.save_candidate("user-1", candidate())
    updated = service.save_candidate("user-1", candidate(content="用户现在更喜欢京剧", confidence=0.99))
    assert updated.memory_id == original.memory_id
    assert repository.get("user-1", original.memory_id).content == "用户现在更喜欢京剧"


def test_low_confidence_inference_is_rejected_but_explicit_fact_is_allowed():
    service, _, _ = make_service()
    service.set_consent("user-1", personalization=True)
    with pytest.raises(ValueError):
        service.save_candidate("user-1", candidate(confidence=0.4, explicitly_stated=False))
    saved = service.save_candidate("user-1", candidate(confidence=0.4, explicitly_stated=True))
    assert saved.confidence == 0.4


def test_sensitive_memory_requires_extra_consent():
    service, _, _ = make_service()
    service.set_consent("user-1", personalization=True)
    with pytest.raises(SensitiveConsentRequiredError):
        service.save_candidate("user-1", candidate(sensitive=True, memory_type="key_event"))
    service.set_consent("user-1", personalization=True, sensitive=True)
    assert service.save_candidate("user-1", candidate(sensitive=True, memory_type="key_event"))


def test_retrieval_filters_expired_and_limits_total_characters():
    service, repository, _ = make_service()
    service.set_consent("user-1", personalization=True)
    active = service.save_candidate("user-1", candidate(content="喜欢散步", source_message_id="m1"))
    expired = service.save_candidate("user-1", candidate(content="过期偏好", source_message_id="m2"))
    expired.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.replace(expired)
    context = service.get_context("user-1", limit=3, max_chars=20)
    assert "喜欢散步" in context
    assert "过期偏好" not in context
    assert len(context) <= 20
    assert active.memory_id


def test_correction_and_deletion_clear_cache_vector_and_body():
    service, repository, vectors = make_service()
    service.set_consent("user-1", personalization=True)
    record = service.save_candidate("user-1", candidate())
    vectors.put(record.memory_id, [0.1, 0.2])
    assert service.get_context("user-1")
    service.correct("user-1", record.memory_id, "用户喜欢听京剧")
    assert not vectors.contains(record.memory_id)
    vectors.put(record.memory_id, [0.2, 0.3])
    service.soft_delete("user-1", record.memory_id)
    tombstone = repository.get("user-1", record.memory_id, include_deleted=True)
    assert tombstone.content == ""
    assert tombstone.deleted_at is not None
    assert not vectors.contains(record.memory_id)
    service.hard_delete("user-1", record.memory_id)
    service.hard_delete("user-1", record.memory_id)
    assert repository.list_for_user("user-1", include_deleted=True) == []
    assert all("用户喜欢" not in repr(item) for item in repository.audit_log)


def test_concurrent_cross_user_access_never_mixes_memories():
    service, _, _ = make_service()
    for user in ("user-a", "user-b"):
        service.set_consent(user, personalization=True)

    def save(user):
        return service.save_candidate(user, candidate(content=f"{user}的偏好", source_message_id=f"msg-{user}"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(save, ("user-a", "user-b")))
    assert "user-a" in service.get_context("user-a")
    assert "user-b" not in service.get_context("user-a")
    assert "user-b" in service.get_context("user-b")


def test_consent_withdrawal_immediately_stops_retrieval_and_clears_cache():
    service, _, _ = make_service()
    service.set_consent("user-1", personalization=True)
    service.save_candidate("user-1", candidate())
    assert service.get_context("user-1")
    assert service.cache_contains("user-1")
    service.set_consent("user-1", personalization=False)
    assert service.get_context("user-1") == ""
    assert not service.cache_contains("user-1")


def test_authorized_memory_context_is_connected_as_untrusted_agent_data():
    service, _, _ = make_service()
    service.set_consent("user-1", personalization=True)
    service.save_candidate("user-1", candidate())

    class MainClient:
        messages = None

        def chat(self, messages, *, json_mode=False):
            self.messages = messages
            return json.dumps({"reply": "我记得你喜欢戏曲。", "intent": "chat", "action": None, "risk_hint": "none"}, ensure_ascii=False)

    class Inspector:
        def chat(self, messages, *, json_mode=False):
            return json.dumps({
                "crisis_detected": False, "safety_violation": False, "intent_accurate": True,
                "age_appropriate": True, "cbt_appropriate": True, "issues": [], "suggestion": "",
                "error_pattern": "none", "lesson": "",
            }, ensure_ascii=False)

    class EmptyKnowledge:
        def context(self, query, top_k=3):
            return "", []

    main = MainClient()
    agent = XiaoliaoAgent(
        Settings(), knowledge_base=EmptyKnowledge(), main_client=main,
        inspector_client=Inspector(), memory_service=service,
    )
    agent.chat("我们聊点什么", user_id="user-1", personalization=True)
    assert "用户喜欢听戏曲" in main.messages[-1]["content"]
    assert "untrusted_memory" in main.messages[-1]["content"]


def test_explicit_age_survives_new_conversation_with_same_user():
    service, repository, _ = make_service()
    service.set_consent("user-age", personalization=True)
    first_main = EchoMainClient()
    first_agent = XiaoliaoAgent(
        Settings(),
        main_client=first_main,
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    first_agent.chat("我今年60岁", user_id="user-age", personalization=True)
    assert "用户年龄：60岁" in service.get_context("user-age")

    second_main = EchoMainClient()
    second_agent = XiaoliaoAgent(
        Settings(),
        main_client=second_main,
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    second_agent.chat("我今年多少岁", user_id="user-age", session_id="new-session", personalization=True)
    assert "用户年龄：60岁" in second_main.messages[-1]["content"]


def test_explicit_personal_facts_survive_new_conversation():
    service, repository, _ = make_service()
    service.set_consent("user-profile", personalization=True)
    first_main = EchoMainClient()
    first_agent = XiaoliaoAgent(
        Settings(),
        main_client=first_main,
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    first_agent.chat(
        "我叫王阿姨，今年60岁，我是沈阳人，退休前是老师，我喜欢听京剧，我儿子叫小明",
        user_id="user-profile",
        personalization=True,
    )
    stored = service.get_context("user-profile", limit=20)
    for snippet in (
        "用户姓名：王阿姨",
        "用户年龄：60岁",
        "用户籍贯：沈阳",
        "用户职业：老师",
        "用户兴趣：听京剧",
        "用户家庭：儿子叫小明",
    ):
        assert snippet in stored

    second_main = EchoMainClient()
    second_agent = XiaoliaoAgent(
        Settings(),
        main_client=second_main,
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    second_agent.chat("你还记得我吗", user_id="user-profile", session_id="new-session", personalization=True)
    assert "王阿姨" in second_main.messages[-1]["content"]
    assert "沈阳" in second_main.messages[-1]["content"]
    assert "听京剧" in second_main.messages[-1]["content"]


def test_explicit_personal_facts_are_saved_even_when_main_model_falls_back():
    service, repository, _ = make_service()
    service.set_consent("user-fallback", personalization=True)
    agent = XiaoliaoAgent(
        Settings(),
        main_client=InvalidMainClient(),
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    agent.chat("我叫王阿姨，今年60岁", user_id="user-fallback", personalization=True)
    stored = service.get_context("user-fallback", limit=10)
    assert "用户姓名：王阿姨" in stored
    assert "用户年龄：60岁" in stored


def test_explicit_age_is_not_saved_without_consent():
    service, repository, _ = make_service()
    main = EchoMainClient()
    agent = XiaoliaoAgent(
        Settings(),
        main_client=main,
        inspector_client=PassingInspector(),
        memory_service=service,
    )
    agent.chat("我今年60岁", user_id="user-no-consent", personalization=False)
    assert service.get_context("user-no-consent") == ""
    assert repository.list_for_user("user-no-consent") == []


def test_personal_facts_extract_age_zodiac_and_occupation():
    agent = XiaoliaoAgent(
        Settings(),
        main_client=EchoMainClient(),
        inspector_client=PassingInspector(),
    )
    facts = agent._extract_personal_facts("我七十多了，我属龙，我是教书的")
    keys = {key for _, key, _ in facts}
    assert "用户年龄：" in keys
    assert "用户生肖：" in keys
    assert "用户职业：" in keys
    assert any("七十多岁" in content for _, _, content in facts)


class SemanticRepository(MemoryMemoryRepository):
    def __init__(self):
        super().__init__()
        self._vector_results: dict[str, float] = {}

    def vector_search(self, user_id, vector, top_k=6):
        return [(memory_id, score) for memory_id, score in self._vector_results.items()][:top_k]

    def update_embedding(self, memory_id, vector):
        self._vector_results[memory_id] = 0.9


class FakeEmbedClient:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_semantic_memory_search_uses_vector_results_when_query_provided():
    repository = SemanticRepository()
    service = MemoryService(repository, embed_client=FakeEmbedClient())
    service.set_consent("user-1", personalization=True)
    record = service.save_candidate("user-1", candidate(content="用户喜欢听京剧"))
    repository._vector_results[record.memory_id] = 0.95
    context = service.get_context("user-1", query="京剧")
    assert "用户喜欢听京剧" in context


def test_backfill_embedding_invalidates_query_cache_for_user():
    repository = SemanticRepository()
    service = MemoryService(repository, embed_client=FakeEmbedClient())
    service.set_consent("user-1", personalization=True)
    record = service.save_candidate("user-1", candidate(content="用户喜欢听京剧"))
    repository._vector_results[record.memory_id] = 0.9
    assert service.get_context("user-1", query="京剧")
    assert "user-1:京剧" in service._cache
    service.backfill_embedding("user-1", record.memory_id, [0.1, 0.2, 0.3])
    assert "user-1:京剧" not in service._cache
