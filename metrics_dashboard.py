"""Local script dashboard over immutable eval runs."""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="eval_runs")
    args = parser.parse_args()
    root = Path(args.out)
    if not root.exists():
        print("尚无评估运行目录")
        return 0
    rows = []
    for run_dir in sorted(root.glob("run-*")):
        manifest_path = run_dir / "manifest.json"
        metrics_path = run_dir / "metrics.json"
        if not manifest_path.exists() or not metrics_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        rows.append({
            "run_id": run_dir.name,
            "mode": manifest.get("mode"),
            "pass_rate": metrics.get("pass_rate"),
            "hard_safety_misses": metrics.get("hard_safety_misses"),
            "p50_latency_ms": metrics.get("p50_latency_ms"),
            "p95_latency_ms": metrics.get("p95_latency_ms"),
            "total_tokens": metrics.get("total_tokens"),
            "cost_total": metrics.get("cost_total"),
            "cost_complete": metrics.get("cost_complete"),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
