import uvicorn

from xiaoliao_agent.config import Settings


def main() -> int:
    settings = Settings.from_env()
    if settings.is_production:
        settings.validate_production()
    if not settings.api_token and not settings.api_test_mode:
        raise RuntimeError("API_TOKEN 未配置，生产 API 拒绝启动")
    host = "0.0.0.0" if settings.is_production else "127.0.0.1"
    uvicorn.run(
        "API服务:app",
        host=host,
        port=settings.api_port,
        workers=settings.api_workers if settings.is_production else 1,
        timeout_graceful_shutdown=settings.api_graceful_shutdown_seconds,
        timeout_keep_alive=settings.api_keep_alive_seconds,
        proxy_headers=bool(settings.api_forwarded_allow_ips),
        forwarded_allow_ips=settings.api_forwarded_allow_ips or "127.0.0.1",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
