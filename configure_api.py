from getpass import getpass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]：").strip()
    return value or default


def main() -> int:
    print("小辽智能体 API 配置")
    print("密钥只写入当前项目的 .env，不会显示在屏幕上，也不会写入 .env.example。")
    print()

    deepseek_base_url = ask("DeepSeek Base URL", "https://api.deepseek.com/v1")
    deepseek_model = ask("DeepSeek 模型 ID", "deepseek-v4-flash")
    deepseek_key = getpass("DeepSeek API Key：").strip()

    qwen_base_url = ask("Qwen Base URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    qwen_model = ask("Qwen 模型 ID", "qwen3.7-max")
    qwen_key = getpass("Qwen API Key：").strip()

    if not deepseek_key or not qwen_key:
        print("配置未保存：两个 API Key 都不能为空。")
        return 2

    content = f"""DEEPSEEK_BASE_URL={deepseek_base_url}
DEEPSEEK_API_KEY={deepseek_key}
DEEPSEEK_MODEL={deepseek_model}

QWEN_BASE_URL={qwen_base_url}
QWEN_API_KEY={qwen_key}
QWEN_MODEL={qwen_model}

AGENT_TEMPERATURE=0.4
AGENT_MAX_TOKENS=800
INSPECTOR_MAX_TOKENS=256
INSPECTOR_ENABLE_THINKING=false
AGENT_TIMEOUT_SECONDS=45
AGENT_TOP_K=3
"""
    temp_path = ENV_PATH.with_suffix(".env.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(ENV_PATH)
    print(f"配置已保存：{ENV_PATH}")
    print("下一步运行“检查API连接.bat”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
