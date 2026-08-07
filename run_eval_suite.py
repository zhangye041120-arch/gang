"""Run the M7 fixed evaluation suite offline or against real models.

Offline mode is deterministic and free. Direct/HTTP modes call real models and
are refused until an explicit cost approval is supplied via --real-approved-by.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import sys
import uuid
from pathlib import Path

from xiaoliao_agent.evaluation import (
    RunManager,
    assert_result,
    compare_reports,
    compute_metrics,
    load_eval_suite,
    require_real_approval,
)


CODE_VERSION = "10-eval-1.0"
PROJECT_ROOT = Path(__file__).resolve().parent


def new_run_id() -> str:
    return "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def suite_hash(suite: dict) -> str:
    canonical = json.dumps(suite, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_manifest(suite: dict, mode: str, *, approved_by: str | None = None, settings=None, endpoint: str = "") -> dict:
    manifest = {
        "run_id": "",
        "code_version": CODE_VERSION,
        "mode": mode,
        "suite_id": suite.get("suite_id", ""),
        "suite_version": suite.get("schema_version", ""),
        "suite_hash": suite_hash(suite),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "approved_by": approved_by,
    }
    if settings is not None:
        manifest["models"] = {
            "main": settings.deepseek_model,
            "inspector": settings.qwen_model,
        }
        manifest["prompt_versions"] = {
            "main-agent": settings.prompt_version,
            "inspector": settings.prompt_version,
            "rewrite": settings.prompt_version,
        }
        manifest["knowledge_version"] = settings.knowledge_version
    return manifest


def _usage_tokens(client) -> dict[str, int | None]:
    usage = getattr(client, "last_usage", None)
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _merge_usage(*usage_items: dict[str, int | None]) -> dict[str, int | None]:
    merged: dict[str, list[int]] = {"input_tokens": [], "output_tokens": [], "total_tokens": []}
    for item in usage_items:
        for key in merged:
            value = item.get(key)
            if isinstance(value, int):
                merged[key].append(value)
    return {
        key: (sum(values) if len(values) == len(usage_items) else None)
        for key, values in merged.items()
    }


def _result_record(case: dict, result: dict, latency_ms: int, *, tokens: dict | None = None, cost: float | None = None) -> dict:
    record = {
        "case_id": case["case_id"],
        "category": case["category"],
        "input": case["input"],
        "context": case["context"],
        "expected_risk": case["expected_risk"],
        "reply": result.get("reply"),
        "intent": result.get("intent"),
        "action": result.get("action"),
        "inspection": result.get("inspection"),
        "blocked": result.get("blocked"),
        "crisis_detected": result.get("crisis_detected"),
        "safety_violation": result.get("safety_violation"),
        "rewritten": result.get("rewritten"),
        "sources": result.get("sources"),
        "latency_ms": latency_ms,
        "error_code": result.get("error_code"),
        "request_id": result.get("request_id", ""),
        "assertions": assert_result(case, result),
        "input_tokens": (tokens or {}).get("input_tokens"),
        "output_tokens": (tokens or {}).get("output_tokens"),
        "total_tokens": (tokens or {}).get("total_tokens"),
        "cost": cost,
    }
    return record


def _error_record(case: dict, error_code: str, detail: str, latency_ms: int) -> dict:
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "input": case["input"],
        "context": case["context"],
        "expected_risk": case["expected_risk"],
        "reply": None,
        "intent": None,
        "action": None,
        "inspection": None,
        "blocked": False,
        "crisis_detected": False,
        "safety_violation": False,
        "rewritten": False,
        "sources": [],
        "latency_ms": latency_ms,
        "error_code": error_code,
        "request_id": "",
        "assertions": [{"name": "run_succeeded", "passed": False, "detail": detail}],
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost": None,
    }


def run_offline(suite: dict, manager: RunManager) -> None:
    from xiaoliao_agent.guardrails import precheck

    records = list(manager.load_partial())
    completed_ids = {record["case_id"] for record in records}
    for case in suite["cases"]:
        if case["case_id"] in completed_ids:
            continue
        actual = precheck(case["input"]).risk_category
        passed = actual == case["expected_risk"]
        records.append({
            "case_id": case["case_id"],
            "category": case["category"],
            "input": case["input"],
            "context": case["context"],
            "expected_risk": case["expected_risk"],
            "reply": None,
            "intent": None,
            "action": None,
            "inspection": None,
            "blocked": actual != "normal",
            "crisis_detected": actual == "crisis",
            "safety_violation": actual in {"medical_boundary", "unsafe_content"},
            "rewritten": False,
            "sources": [],
            "latency_ms": 0,
            "error_code": None,
            "request_id": "",
            "assertions": [{
                "name": "deterministic_risk_precheck",
                "passed": passed,
                "detail": f"expected={case['expected_risk']}, actual={actual}",
            }],
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost": None,
        })
        manager.save_partial(records, "running")
    metrics = compute_metrics(records)
    manager.finalize(
        records,
        metrics,
        status="offline_completed",
        metadata={"endpoint": "offline-precheck"},
    )


def run_direct(suite: dict, manager: RunManager, timeout_seconds: int) -> None:
    from xiaoliao_agent.agent import XiaoliaoAgent
    from xiaoliao_agent.config import Settings

    settings = Settings.from_env()
    settings.validate_live()
    agent = XiaoliaoAgent(settings)
    records = list(manager.load_partial())
    completed_ids = {record["case_id"] for record in records}

    for case in suite["cases"]:
        if case["case_id"] in completed_ids:
            continue
        started = datetime.now(timezone.utc)
        request_id = uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                agent.chat,
                case["input"],
                [],
                case["context"].get("user_summary", ""),
                f"eval-{case['case_id']}",
                f"eval-session-{case['case_id']}",
                bool(case["context"].get("consent", {}).get("personalization", False)),
                request_id=request_id,
            )
            try:
                result = future.result(timeout=timeout_seconds)
            except TimeoutError:
                latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
                records.append(_error_record(case, "EVAL_TIMEOUT", "模型调用超时", latency_ms))
                manager.save_partial(records, "running")
                continue
            except Exception as exc:
                latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
                records.append(_error_record(case, "EVAL_REQUEST_FAILED", str(exc)[:500], latency_ms))
                manager.save_partial(records, "running")
                continue
        latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        tokens = _merge_usage(_usage_tokens(agent.main_client), _usage_tokens(agent.inspector_client))
        result_dict = {
            "reply": result.reply,
            "intent": result.intent,
            "action": result.action,
            "inspection": result.to_dict().get("inspection", {}),
            "blocked": result.blocked,
            "crisis_detected": result.crisis_detected,
            "safety_violation": result.safety_violation,
            "rewritten": result.rewritten,
            "sources": result.sources,
            "error_code": result.error_code,
            "request_id": result.request_id,
        }
        records.append(_result_record(case, result_dict, latency_ms, tokens=tokens, cost=None))
        manager.save_partial(records, "running")
    metrics = compute_metrics(records)
    manager.finalize(records, metrics, status="completed", metadata={"endpoint": "direct (agent.chat)"})


def run_http(suite: dict, manager: RunManager, base_url: str, token: str, timeout_seconds: int) -> None:
    import urllib.error
    import urllib.request

    records = list(manager.load_partial())
    completed_ids = {record["case_id"] for record in records}
    for case in suite["cases"]:
        if case["case_id"] in completed_ids:
            continue
        started = datetime.now(timezone.utc)
        payload = json.dumps({
            "user_id": f"eval-{case['case_id']}",
            "session_id": f"eval-session-{case['case_id']}",
            "message": case["input"],
            "context": case["context"],
            "debug": False,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/v1/chat",
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
            records.append(_error_record(case, f"EVAL_HTTP_{exc.code}", str(exc)[:500], latency_ms))
            manager.save_partial(records, "running")
            continue
        except Exception as exc:
            latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
            records.append(_error_record(case, "EVAL_REQUEST_FAILED", str(exc)[:500], latency_ms))
            manager.save_partial(records, "running")
            continue
        latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        result_dict = {
            "reply": body.get("reply"),
            "intent": body.get("intent"),
            "action": body.get("action"),
            "inspection": (body.get("debug") or {}).get("inspection"),
            "blocked": body.get("blocked"),
            "crisis_detected": body.get("crisis_detected"),
            "safety_violation": body.get("safety_violation"),
            "rewritten": body.get("rewritten"),
            "sources": [],
            "error_code": None,
            "request_id": body.get("request_id", ""),
        }
        records.append(_result_record(case, result_dict, latency_ms))
        manager.save_partial(records, "running")
    metrics = compute_metrics(records)
    manager.finalize(records, metrics, status="completed", metadata={"endpoint": f"POST {base_url}/v1/chat"})


def compare_runs(first_dir: str, second_dir: str) -> None:
    first_path = Path(first_dir) / "results.json"
    second_path = Path(second_dir) / "results.json"
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    first["run_id"] = Path(first_dir).name
    second["run_id"] = Path(second_dir).name
    first["manifest"] = json.loads((Path(first_dir) / "manifest.json").read_text(encoding="utf-8"))
    second["manifest"] = json.loads((Path(second_dir) / "manifest.json").read_text(encoding="utf-8"))
    first["metrics"] = json.loads((Path(first_dir) / "metrics.json").read_text(encoding="utf-8"))
    second["metrics"] = json.loads((Path(second_dir) / "metrics.json").read_text(encoding="utf-8"))
    print(json.dumps(compare_reports(first, second), ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 fixed evaluation suite")
    parser.add_argument("--offline", action="store_true", help="run deterministic prechecks without models")
    parser.add_argument("--direct", action="store_true", help="run real models through XiaoliaoAgent directly")
    parser.add_argument("--http", action="store_true", help="run real models through POST /v1/chat")
    parser.add_argument("--suite", default=str(PROJECT_ROOT / "eval_suite_v1.json"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "eval_runs"))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume", action="store_true", help="resume an existing partial run directory")
    parser.add_argument("--real-approved-by", default="", help="required for direct/http; records the approver")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    args = parser.parse_args()

    if args.compare:
        compare_runs(args.compare[0], args.compare[1])
        return 0

    if args.offline and (args.direct or args.http):
        print("offline 不能与 direct/http 同时使用", file=sys.stderr)
        return 2
    mode = "offline" if args.offline else ("direct" if args.direct else "http")
    if mode != "offline":
        require_real_approval(args.real_approved_by)

    suite = load_eval_suite(args.suite)
    run_id = args.run_id or new_run_id()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)

    if mode == "offline":
        endpoint = "offline-precheck"
        settings = None
    elif mode == "direct":
        endpoint = "direct (agent.chat)"
        from xiaoliao_agent.config import Settings
        settings = Settings.from_env()
    else:
        endpoint = f"POST {args.base_url}/v1/chat"
        from xiaoliao_agent.config import Settings
        settings = Settings.from_env()

    manifest = build_manifest(suite, mode, approved_by=args.real_approved_by or None, settings=settings, endpoint=endpoint)
    manifest["run_id"] = run_id
    manager = RunManager(root, run_id, manifest=manifest)
    if args.resume:
        if not manager.manifest_path.exists():
            manager.start()
        manager.load_partial()
    else:
        manager.start()

    try:
        if mode == "offline":
            run_offline(suite, manager)
        elif mode == "direct":
            run_direct(suite, manager, args.timeout)
        else:
            run_http(suite, manager, args.base_url, args.token, args.timeout)
    except Exception as exc:
        print(f"评估中断：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{manager.run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
