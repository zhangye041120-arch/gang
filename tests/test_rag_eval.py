import json
from pathlib import Path

import pytest

from run_rag_eval import evaluate_offline, load_rag_cases, save_rag_run
from xiaoliao_agent.config import Settings
from xiaoliao_agent.knowledge import KnowledgeBase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_kb():
    settings = Settings()
    return KnowledgeBase.from_files(
        settings.knowledge_path,
        settings.lessons_path,
        settings.elder_scenarios_path,
        settings.regional_resources_path,
        version=settings.knowledge_version,
    )


def test_rag_cases_load_from_v2_regression_file():
    cases = load_rag_cases()
    assert len(cases) >= 20
    assert {case.case_id for case in cases} >= {
        "cbt_low_mood",
        "elder_spouse_loss",
        "resource_medicine_warning",
        "lesson_relation_feedback",
        "edge_empty",
    }


def test_offline_rag_eval_is_green_and_reproducible():
    first = evaluate_offline(make_kb())
    second = evaluate_offline(make_kb())
    assert first.status == "completed"
    assert first.passed == first.total_cases
    assert first.overall_hit_rate > 0.7
    assert [case["passed"] for case in first.cases] == [case["passed"] for case in second.cases]


def test_rag_run_output_is_immutable(tmp_path):
    report = evaluate_offline(make_kb())
    first_path = save_rag_run(report, output_root=tmp_path, run_id="run-rag-a")
    assert first_path.exists()
    with pytest.raises(FileExistsError):
        save_rag_run(report, output_root=tmp_path, run_id="run-rag-a")
    manifest = json.loads((tmp_path / "run-rag-a" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
