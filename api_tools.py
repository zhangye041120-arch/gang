"""API 运维工具：配置模型密钥、生成 API Token、检查真实模型连接。"""

import argparse
from getpass import getpass
from pathlib import Path
import re
import secrets

from xiaoliao_agent.providers import OpenAICompatibleClient
from xiaoliao_agent.config import Settings


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def _ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]：").strip()
    return value or default


def _configure() -> int:
    print("小辽智能体 API 配置")
    print("密钥只写入当前项目的 .env，不会显示在屏幕上，也不会写入 .env.example。")
    print()

    deepseek_base_url = _ask("DeepSeek Base URL", "https://api.deepseek.com/v1")
    deepseek_model = _ask("DeepSeek 模型 ID", "deepseek-v4-flash")
    deepseek_key = getpass("DeepSeek API Key：").strip()

    qwen_base_url = _ask("Qwen Base URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    qwen_model = _ask("Qwen 模型 ID", "qwen3.7-max")
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


def _token() -> int:
    content = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    match = re.search(r"^API_TOKEN=(.*)$", content, flags=re.M)
    if match and match.group(1).strip():
        print("API_TOKEN 已存在，未覆盖。")
        return 0
    line = "API_TOKEN=" + secrets.token_urlsafe(48)
    if match:
        content = content[: match.start()] + line + content[match.end() :]
    else:
        content = content.rstrip() + "\n" + line + "\n"
    ENV_PATH.write_text(content, encoding="utf-8")
    print("API_TOKEN 已生成并写入 .env，未显示其值。")
    return 0


def _check_client(name: str, client: OpenAICompatibleClient) -> bool:
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


def _check() -> int:
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
    deepseek_ok = _check_client(f"DeepSeek ({settings.deepseek_model})", deepseek)
    qwen_ok = _check_client(f"Qwen ({settings.qwen_model})", qwen)
    if deepseek_ok and qwen_ok:
        print("两个真实模型接口都已连接成功。现在可以运行“开始真实模型对话.bat”。")
        return 0
    print("至少一个接口连接失败。请检查 Key、Base URL、模型 ID、余额和地域。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="小辽 API 运维工具")
    parser.add_argument("command", choices=("configure", "token", "check"))
    args = parser.parse_args()
    if args.command == "configure":
        return _configure()
    if args.command == "token":
        return _token()
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())
