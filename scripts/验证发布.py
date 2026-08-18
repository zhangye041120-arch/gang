from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def assert_clean_worktree(status: str) -> None:
    if status.strip():
        raise RuntimeError("release verification requires a clean worktree")


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a release candidate")
    parser.add_argument("--check", action="store_true", help="run fast contract checks")
    parser.add_argument("--gate", action="store_true", help="run the complete local release gate")
    args = parser.parse_args()
    if not args.check and not args.gate:
        parser.error("one of --check or --gate is required")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert_clean_worktree(status)
    run([sys.executable, "生成OpenAPI.py", "--check"])
    run([sys.executable, "-m", "compileall", "-q", "xiaoliao_agent", "API服务.py"])
    run(["docker", "compose", "config", "-q"])
    if args.gate:
        run([sys.executable, "-m", "pytest", "-q", "--tb=short"])
        run([sys.executable, "运行评估套件.py", "--offline", "--gate", "--out", str(ROOT / "eval_runs")])
        run([sys.executable, "运行RAG评估.py", "--offline", "--gate", "--output", str(ROOT / "rag_runs")])
        run(["pip-audit", "--no-deps", "--disable-pip", "-r", "requirements.txt"])
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.splitlines()
        run(["detect-secrets-hook", "--baseline", ".secrets.baseline", *tracked])
        run(["docker", "build", "--check", "."])
        run(["docker", "build", "--tag", "xiaoliao-agent:release-verify", "."])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
