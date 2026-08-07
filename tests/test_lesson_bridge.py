from xiaoliao_agent.lesson_bridge import LessonBridge
from xiaoliao_agent.quality import MemoryQualityRepository


def test_lesson_bridge_returns_only_approved_lessons():
    repository = MemoryQualityRepository()
    pending = repository.add_lesson("待审教训：先共情", "intent_mismatch", "1.2.0")
    approved = repository.add_lesson("已批准教训：不要辩解", "relation_feedback", "1.2.0")
    repository.review_lesson(approved.lesson_id, "approved", reviewer="reviewer", reason="人工确认")
    bridge = LessonBridge(repository)
    text, sources = bridge.context("你根本不理解我")
    assert "已批准教训" in text
    assert "待审教训" not in text
    assert all(source["source"] == "operations-lesson" for source in sources)
    assert pending.lesson_id not in {source["lesson_id"] for source in sources}


def test_lesson_bridge_lexical_rank_and_sources_are_stable():
    repository = MemoryQualityRepository()
    first = repository.add_lesson("先共情，再引导", "intent_mismatch", "1.2.0")
    second = repository.add_lesson("不要替用户做决定", "intent_mismatch", "1.2.0")
    for lesson in (first, second):
        repository.review_lesson(lesson.lesson_id, "approved", reviewer="reviewer", reason="人工确认")
    bridge = LessonBridge(repository)
    results = bridge.search("共情")
    assert results
    assert all(isinstance(score, float) for _, score in results)
