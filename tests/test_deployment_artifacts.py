from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_is_digest_pinned_non_root_and_hash_locked():
    dockerfile = read("Dockerfile")

    assert "@sha256:" in dockerfile
    assert " AS builder" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "--reload" not in dockerfile
    assert "COPY ." in dockerfile


def test_compose_has_isolated_services_health_dependencies_and_hardening():
    compose = yaml.safe_load(read("docker-compose.yml"))
    services = compose["services"]

    assert set(services) == {
        "postgres", "redis", "migrate", "agent-api", "checkin-worker"
    }
    assert "ports" not in services["agent-api"]
    assert services["postgres"]["healthcheck"]
    assert services["redis"]["healthcheck"]
    assert "--appendonly" in " ".join(services["redis"]["command"])
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["agent-api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["checkin-worker"]["command"] != services["agent-api"]["command"]
    assert set(services["postgres"]["networks"]) == {"backend"}
    assert set(services["redis"]["networks"]) == {"backend"}
    assert set(services["agent-api"]["networks"]) == {"backend", "egress"}
    assert set(services["checkin-worker"]["networks"]) == {"backend", "egress"}
    for name in ("migrate", "agent-api", "checkin-worker"):
        service = services[name]
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["tmpfs"]


def test_dockerignore_excludes_secrets_data_logs_and_git():
    ignored = read(".dockerignore")
    for pattern in (
        ".env", ".env.*", ".git", "*.log", "data", "eval_outputs",
        "__pycache__", ".pytest_cache",
    ):
        assert pattern in ignored


def assert_hash_locked(name):
    content = read(name)
    requirement_lines = [
        line for line in content.splitlines()
        if line and not line.startswith(("#", " ", "-"))
    ]
    assert requirement_lines
    assert content.count("--hash=sha256:") >= len(requirement_lines)


def test_runtime_and_dev_requirements_are_separate_hash_locks():
    assert "pytest" not in read("requirements.in")
    assert "pytest" in read("requirements-dev.in")
    assert_hash_locked("requirements.txt")
    assert_hash_locked("requirements-dev.txt")


def test_api_runner_uses_production_worker_and_timeout_settings():
    runner = read("启动API.py")

    assert "settings.api_workers" in runner
    assert "settings.api_graceful_shutdown_seconds" in runner
    assert "settings.api_keep_alive_seconds" in runner
    assert "settings.api_forwarded_allow_ips" in runner
    assert "reload=True" not in runner
