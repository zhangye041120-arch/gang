from pathlib import Path
import json

from xiaoliao_agent.knowledge import KnowledgeBase, _source_name, split_markdown
from xiaoliao_agent.knowledge_repository import MemoryKnowledgeRepository
from xiaoliao_agent.knowledge_import import import_files


def test_split_markdown_creates_chunks():
    chunks = split_markdown("## 情绪\n\n情绪命名和识别自动思维。\n### 行动\n\n行为激活。")
    assert chunks
    assert any("行为激活" in chunk.content for chunk in chunks)


def test_retrieval_finds_cbt_topic():
    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md")
    results = kb.search("我什么都不想做 行为激活", top_k=3)
    assert results
    assert any("行为" in chunk.content or "行动" in chunk.content for chunk, _ in results)


def test_chunk_ids_are_unique_across_multiple_sources(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("## 第一来源\n\n行为激活。", encoding="utf-8")
    second.write_text("## 第二来源\n\n安全评估。", encoding="utf-8")
    kb = KnowledgeBase.from_files(first, second)
    ids = [chunk.chunk_id for chunk in kb.chunks]
    assert len(ids) == len(set(ids))


def test_chunks_have_stable_metadata_and_heading_path(tmp_path):
    source = tmp_path / "cbt.md"
    source.write_text("## 情绪\n### 退缩\n\n行为激活。", encoding="utf-8")
    first = KnowledgeBase.from_files(source, version="2026.08")
    second = KnowledgeBase.from_files(source, version="2026.08")
    assert first.chunks[0].chunk_id == second.chunks[0].chunk_id
    assert first.chunks[0].source == "cbt"
    assert first.chunks[0].version == "2026.08"
    assert first.chunks[0].heading_path == ("情绪", "退缩")
    assert first.chunks[0].content_hash


def test_import_is_idempotent_for_same_content(tmp_path):
    source = tmp_path / "cbt.md"
    source.write_text("## 安全评估\n\n先确认当下安全。", encoding="utf-8")
    repository = MemoryKnowledgeRepository()
    first = import_files(repository, [source], version="v1")
    second = import_files(repository, [source], version="v1")
    assert first.inserted == 1
    assert second.skipped == 1
    assert len(repository.rows) == 1


def test_import_prunes_removed_sources(tmp_path):
    keep = tmp_path / "keep.md"
    removed = tmp_path / "removed.md"
    keep.write_text("## CBT\n\n情绪识别。", encoding="utf-8")
    removed.write_text("## 旧文档\n\n旧内容。", encoding="utf-8")
    repository = MemoryKnowledgeRepository()
    import_files(repository, [keep, removed], version="v1")
    assert len(repository.rows) == 2

    removed.unlink()
    stats = import_files(repository, [keep], version="v1", prune=True)
    assert stats.pruned == 1
    assert len(repository.rows) == 1
    assert next(iter(repository.rows.values())).source == "keep"


def test_vector_failure_falls_back_to_lexical_search():
    def broken_vector_search(query, top_k):
        raise RuntimeError("embedding unavailable")

    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md", vector_search=broken_vector_search)
    results = kb.search("行为激活", top_k=2)
    assert results
    assert kb.last_search_error["type"] == "vector_search_unavailable"


def test_rerank_reorders_retrieved_chunks():
    chunks = split_markdown(
        "## 甲\n\n苹果很好吃。\n## 乙\n\n香蕉很甜。\n## 丙\n\n橘子很酸。",
        source="kb",
    )
    ids = [chunk.chunk_id for chunk in chunks]

    def vector_search(query, top_k):
        return [(ids[0], 0.9), (ids[1], 0.8), (ids[2], 0.7)][:top_k]

    def rerank(query, documents):
        return [0.1, 0.9, 0.8]

    kb = KnowledgeBase(chunks, vector_search=vector_search, rerank=rerank, rerank_candidates=2)
    results = kb.search("zzzz", top_k=2)
    assert results[0][0].chunk_id == ids[1]
    assert "香蕉" in results[0][0].content


def test_rerank_failure_falls_back_to_hybrid():
    chunks = split_markdown(
        "## 甲\n\n苹果很好吃。\n## 乙\n\n香蕉很甜。",
        source="kb",
    )
    ids = [chunk.chunk_id for chunk in chunks]

    def vector_search(query, top_k):
        return [(ids[0], 0.9), (ids[1], 0.8)][:top_k]

    def broken_rerank(query, documents):
        raise RuntimeError("rerank down")

    kb = KnowledgeBase(chunks, vector_search=vector_search, rerank=broken_rerank)
    results = kb.search("zzzz", top_k=2)
    assert results
    assert kb.last_search_error["type"] == "rerank_unavailable"


def test_search_results_include_provenance_metadata():
    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md")
    _, sources = kb.context("安全评估", top_k=1)
    assert sources
    assert {"chunk_id", "source", "version", "heading_path", "score"} <= set(sources[0])


def test_rag_regression_cases_are_machine_checkable():
    regression = json.loads((Path(__file__).parents[1] / "rag_cases.json").read_text(encoding="utf-8"))
    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md")
    cbt_cases = [case for case in regression["cases"] if case.get("category") == "cbt"]
    assert len(cbt_cases) == 6
    for case in cbt_cases:
        results = kb.search(case["query"], top_k=5)
        text = "\n".join(chunk.content for chunk, _ in results)
        assert any(term in text for term in case["expected_terms"])


def test_import_uses_stable_source_name_for_pure_cjk_file(tmp_path):
    source = tmp_path / "纯中文场景.md"
    source.write_text("## 退休适应\n\n退休后价值感流失。", encoding="utf-8")
    repository = MemoryKnowledgeRepository()
    import_files(repository, [source], version="v1")
    chunk = next(iter(repository.rows.values()))
    assert chunk.source == _source_name("纯中文场景")


def test_knowledge_loads_only_cbt_and_lessons():
    root = Path(__file__).parents[1]
    kb = KnowledgeBase.from_files(
        root / "knowledge" / "CBT知识库_Agent版.md",
        root / "knowledge" / "lessons.md",
    )
    sources = {chunk.source for chunk in kb.chunks}
    assert {"cbt-agent", "lessons"} <= sources
    assert len(sources) == 2
