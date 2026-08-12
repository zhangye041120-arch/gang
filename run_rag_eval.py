"""RAG专项评估：衡量检索召回率、命中率和排序质量。

Usage:
    python run_rag_eval.py                          # 离线评估
    python run_rag_eval.py --real --approved-by "负责人"  # 含 embedding 的完整评估
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent
RAG_CASES_PATH = PROJECT_ROOT / "rag_cases.json"
CODE_VERSION = "rag-eval-1.0"


@dataclass
class RAGCase:
    case_id: str
    query: str
    category: str
    expected_terms: list[str]
    min_hits_expected: int = 1
    forbidden_terms: list[str] = field(default_factory=list)
    severity: str = "normal"


def load_rag_cases(path: str | Path | None = None) -> list[RAGCase]:
    source = Path(path or RAG_CASES_PATH)
    data = json.loads(source.read_text(encoding="utf-8"))
    cases: list[RAGCase] = []
    for item in data["cases"]:
        cases.append(RAGCase(
            case_id=str(item["case_id"]),
            query=str(item["query"]),
            category=str(item.get("category", "unknown")),
            expected_terms=list(item.get("expected_terms", [])),
            min_hits_expected=int(item.get("min_hits", item.get("min_hits_expected", 1))),
            forbidden_terms=list(item.get("forbidden_terms", [])),
            severity=str(item.get("severity", "normal")),
        ))
    return cases


RAG_CASES = load_rag_cases()


@dataclass
class RAGEvalReport:
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "running"
    total_cases: int = len(RAG_CASES)
    passed: int = 0
    failed: int = 0
    overall_hit_rate: float = 0.0
    category_stats: dict = field(default_factory=dict)
    cases: list[dict] = field(default_factory=list)


def evaluate_offline(knowledge_base, lesson_bridge=None) -> RAGEvalReport:
    """Deterministic evaluation — no model calls, just retrieval metrics."""
    report = RAGEvalReport()
    category_results: dict[str, list[dict]] = {}
    for case in RAG_CASES:
        started = time.perf_counter()
        query = case.query.strip()
        if not query:
            retrieved_text, sources = "", []
        else:
            retrieved_text, sources = knowledge_base.context(query, top_k=5)
            if lesson_bridge is not None:
                lesson_text, lesson_srcs = lesson_bridge.context(query)
                if lesson_text:
                    retrieved_text += "\n\n" + lesson_text
                    sources = sources + lesson_srcs

        latency_ms = int((time.perf_counter() - started) * 1000)
        retrieved_lower = retrieved_text.lower()
        hits = [term for term in case.expected_terms if term.lower() in retrieved_lower]
        misses = [term for term in case.expected_terms if term.lower() not in retrieved_lower]
        forbidden_found = [term for term in case.forbidden_terms if term.lower() in retrieved_lower]
        hit_rate = len(hits) / max(len(case.expected_terms), 1)
        passed = len(hits) >= case.min_hits_expected and not forbidden_found

        case_result = {
            "case_id": case.case_id,
            "query": query,
            "category": case.category,
            "passed": passed,
            "hit_count": len(hits),
            "total_terms": len(case.expected_terms),
            "hit_rate": round(hit_rate, 3),
            "hits": hits,
            "misses": misses,
            "forbidden_found": forbidden_found,
            "chunks_retrieved": len(sources),
            "top_score": round(sources[0]["score"], 4) if sources else 0,
            "latency_ms": latency_ms,
            "sources": [
                {
                    "heading": source.get("heading", source.get("source", "")),
                    "heading_path": source.get("heading_path", []),
                    "source": source.get("source", ""),
                    "score": source.get("score", 0),
                }
                for source in sources[:3]
            ],
        }
        report.cases.append(case_result)
        category_results.setdefault(case.category, []).append(case_result)
        if passed:
            report.passed += 1
        else:
            report.failed += 1

    for category, results in category_results.items():
        report.category_stats[category] = {
            "cases": len(results),
            "passed": sum(1 for item in results if item["passed"]),
            "avg_hit_rate": round(sum(item["hit_rate"] for item in results) / len(results), 3),
            "avg_chunks": round(sum(item["chunks_retrieved"] for item in results) / len(results), 1),
        }
    report.status = "completed" if report.failed == 0 else "partial_failures"
    report.overall_hit_rate = round(
        sum(item["hit_rate"] for item in report.cases) / max(len(report.cases), 1), 3
    )
    return report


def report_to_dict(report: RAGEvalReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "status": report.status,
        "total_cases": report.total_cases,
        "passed": report.passed,
        "failed": report.failed,
        "overall_hit_rate": report.overall_hit_rate,
        "category_stats": report.category_stats,
        "cases": report.cases,
    }


def save_rag_run(
    report: RAGEvalReport,
    *,
    output_root: str | Path,
    run_id: str | None = None,
) -> Path:
    root = Path(output_root)
    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = root / run_id
    results_path = run_dir / "results.json"
    if results_path.exists():
        raise FileExistsError(f"RAG 运行结果已存在且不可覆盖：{results_path}")
    run_dir.mkdir(parents=True, exist_ok=True)
    data = report_to_dict(report)
    (run_dir / "manifest.json").write_text(
        json.dumps({
            "run_id": run_id,
            "code_version": CODE_VERSION,
            "cases_source": str(RAG_CASES_PATH),
            "generated_at": report.generated_at,
            "status": report.status,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps({
            "overall_hit_rate": report.overall_hit_rate,
            "passed": report.passed,
            "failed": report.failed,
            "category_stats": report.category_stats,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results_path


def print_report(report: RAGEvalReport) -> None:
    print(f"\n{'='*60}")
    print(f"RAG 评估结果: {report.status}")
    print(f"总用例: {report.total_cases}  |  通过: {report.passed}  |  失败: {report.failed}")
    print(f"整体命中率: {report.overall_hit_rate:.3f}")
    for category, stats in sorted(report.category_stats.items()):
        flag = "OK" if stats["passed"] == stats["cases"] else "FAIL"
        print(f"  {flag} {category}: {stats['passed']}/{stats['cases']} passed, "
              f"hit_rate={stats['avg_hit_rate']:.3f}, avg_chunks={stats['avg_chunks']}")
    for case in report.cases:
        if not case["passed"]:
            print(f"  FAIL {case['case_id']} ({case['category']}): "
                  f"hits={case['hits']}, misses={case['misses']}, forbidden={case['forbidden_found']}")
    print(f"{'='*60}")


def make_knowledge_base():
    from xiaoliao_agent.config import Settings
    from xiaoliao_agent.knowledge import KnowledgeBase

    settings = Settings.from_env()
    return KnowledgeBase.from_files(
        settings.knowledge_path,
        settings.lessons_path,
        version=settings.knowledge_version,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 专项评估")
    parser.add_argument("--offline", action="store_true", help="offline lexical retrieval only")
    parser.add_argument("--real", action="store_true", help="run with embedding clients")
    parser.add_argument("--approved-by", default="", help="required for --real")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "rag_runs"))
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    if not args.offline and not args.real:
        args.offline = True
    if args.real and not args.approved_by.strip():
        print("错误：真实模型评估须显式批准。使用 --approved-by \"负责人姓名\"", file=sys.stderr)
        return 2

    kb = make_knowledge_base()
    if args.real:
        from xiaoliao_agent.agent import XiaoliaoAgent
        from xiaoliao_agent.config import Settings

        settings = Settings.from_env()
        settings.validate_live()
        agent = XiaoliaoAgent(settings)
        report = evaluate_offline(agent.kb, agent.lesson_bridge)
    else:
        report = evaluate_offline(kb)

    print_report(report)
    results_path = save_rag_run(report, output_root=args.output, run_id=args.run_id or None)
    print(f"\n结果已保存: {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
