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


def test_vector_failure_falls_back_to_lexical_search():
    def broken_vector_search(query, top_k):
        raise RuntimeError("embedding unavailable")

    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md", vector_search=broken_vector_search)
    results = kb.search("行为激活", top_k=2)
    assert results
    assert kb.last_search_error["type"] == "vector_search_unavailable"


def test_search_results_include_provenance_metadata():
    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md")
    _, sources = kb.context("安全评估", top_k=1)
    assert sources
    assert {"chunk_id", "source", "version", "heading_path", "score"} <= set(sources[0])


def test_rag_regression_cases_are_machine_checkable():
    regression = json.loads((Path(__file__).parents[1] / "knowledge" / "rag_regression_v1.json").read_text(encoding="utf-8"))
    kb = KnowledgeBase.from_files(Path(__file__).parents[1] / "knowledge" / "CBT知识库_Agent版.md")
    assert len(regression["cases"]) == 6
    for case in regression["cases"]:
        results = kb.search(case["query"], top_k=5)
        text = "\n".join(chunk.content for chunk, _ in results)
        assert any(term in text for term in case["expected_terms"])


def test_import_uses_stable_source_name_for_pure_cjk_file(tmp_path):
    source = tmp_path / "适老生活场景.md"
    source.write_text("## 退休适应\n\n退休后价值感流失。", encoding="utf-8")
    repository = MemoryKnowledgeRepository()
    import_files(repository, [source], version="v1")
    chunk = next(iter(repository.rows.values()))
    assert chunk.source == _source_name("适老生活场景")


def test_knowledge_expansion_loads_elder_and_regional_sources():
    root = Path(__file__).parents[1]
    kb = KnowledgeBase.from_files(
        root / "knowledge" / "CBT知识库_Agent版.md",
        root / "knowledge" / "适老生活场景.md",
        root / "knowledge" / "地区资源参考.md",
        root / "knowledge" / "lessons.md",
    )
    assert len(kb.chunks) >= 99
    sources = {chunk.source for chunk in kb.chunks}
    assert len(sources) >= 4


def test_regional_resources_do_not_embed_phone_numbers():
    text = (Path(__file__).parents[1] / "knowledge" / "地区资源参考.md").read_text(encoding="utf-8")
    assert "1[3-9]\\d{9}" not in text
    assert "待确认" in text or "替换" in text or "不能保证" in text
