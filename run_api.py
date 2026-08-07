import uvicorn

from xiaoliao_agent.config import Settings


def main() -> int:
    settings = Settings.from_env()
    if not settings.api_token and not settings.api_test_mode:
        raise RuntimeError("API_TOKEN 未配置，生产 API 拒绝启动")
    print(f"小辽智能体 API: http://127.0.0.1:{settings.api_port}/v1/chat")
    print(f"接口文档: http://127.0.0.1:{settings.api_port}/docs")
    uvicorn.run("api_server:app", host="127.0.0.1", port=settings.api_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
