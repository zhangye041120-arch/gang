from pathlib import Path
import re
import secrets


ENV_PATH = Path(__file__).resolve().parent / ".env"


def main() -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
