from xiaoliao_agent.client import OpenAICompatibleClient
from xiaoliao_agent.config import Settings


def check(name: str, client: OpenAICompatibleClient) -> bool:
    print(f"正在检查 {name} ...")
    try:
        reply = client.chat([
            {"role": "system", "content": "这是连接测试。"},
            {"role": "user", "content": "请只回复 OK"},
        ])
    except Exception as exc:
        print(f"[失败] {name}: {exc}")
        return False
    print(f"[成功] {name}: {reply[:120]}")
    return True


def main() -> int:
    settings = Settings.from_env()
    try:
        settings.validate_live()
    except RuntimeError as exc:
        print(exc)
        print("请先运行“配置API.bat”。")
        return 2

    deepseek = OpenAICompatibleClient(
        settings.deepseek_base_url,
        settings.deepseek_api_key,
        settings.deepseek_model,
        settings,
    )
    qwen = OpenAICompatibleClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.qwen_model,
        settings,
    )
    deepseek_ok = check(f"DeepSeek ({settings.deepseek_model})", deepseek)
    qwen_ok = check(f"Qwen ({settings.qwen_model})", qwen)
    if deepseek_ok and qwen_ok:
        print("两个真实模型接口都已连接成功。现在可以运行“开始真实模型对话.bat”。")
        return 0
    print("至少一个接口连接失败。请检查 Key、Base URL、模型 ID、余额和地域。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
