from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_ci_uses_immutable_actions_and_all_release_checks():
    workflow = yaml.safe_load(read(".github/workflows/ci.yml"))
    rendered = read(".github/workflows/ci.yml")
    uses = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]

    assert uses
    assert all(len(value.rsplit("@", 1)[-1]) == 40 for value in uses)
    for required in (
        "pytest", "compileall", "运行评估套件.py --offline --gate",
        "运行RAG评估.py --offline --gate", "生成OpenAPI.py --check",
        "pip-audit", "detect-secrets", "docker build --check",
        "docker build",
    ):
        assert required in rendered
    assert "postgres:" in rendered
    assert "redis:" in rendered
    assert "password:" not in rendered.lower()


def test_backup_and_restore_validate_paths_and_target_database(tmp_path):
    from scripts.备份数据库 import validate_destination
    from scripts.恢复数据库 import validate_restore

    destination = tmp_path / "backups"
    destination.mkdir()
    assert validate_destination(destination) == destination.resolve()
    with pytest.raises(ValueError):
        validate_destination(Path("."))

    backup = destination / "xiaoliao-20260817T000000Z.dump.age"
    backup.write_bytes(b"encrypted")
    assert validate_restore(backup, "xiaoliao_restore", "xiaoliao") == backup.resolve()
    with pytest.raises(ValueError):
        validate_restore(backup, "xiaoliao", "xiaoliao")
    with pytest.raises(ValueError):
        validate_restore(destination / "other.dump", "restore", "xiaoliao")


def test_release_scripts_use_argument_subprocess_without_shell():
    for name in (
        "scripts/备份数据库.py",
        "scripts/恢复数据库.py",
        "scripts/验证发布.py",
    ):
        source = read(name)
        assert "subprocess.run(" in source
        assert "shell=True" not in source


def test_release_gate_rejects_dirty_or_untracked_artifacts():
    from scripts.验证发布 import assert_clean_worktree

    assert_clean_worktree("")
    with pytest.raises(RuntimeError):
        assert_clean_worktree(" M API服务.py")
    with pytest.raises(RuntimeError):
        assert_clean_worktree("?? untracked.txt")


def test_openapi_check_mode_matches_frozen_file():
    result = subprocess.run(
        [sys.executable, "生成OpenAPI.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_java_gateway_hmac_example_matches_frozen_header_contract():
    source = read("examples/java/GatewayHmacSigner.java")

    assert "HmacSHA256" in source
    assert "X-Gateway-Timestamp" in source
    assert "X-Gateway-Nonce" in source
    assert "X-Gateway-User-ID" in source
    assert "X-Gateway-Signature" in source
    assert 'String.join("\\n"' in source
    assert "SHA-256" in source
