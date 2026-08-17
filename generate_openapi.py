import argparse
import json
from pathlib import Path

from api_server import app


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify frozen OpenAPI")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parent / "openapi.json"
    generated = json.dumps(app.openapi(), ensure_ascii=False, indent=2)
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != generated:
            print("openapi.json 与运行时合同不一致")
            return 1
        print("openapi.json 合同一致")
        return 0
    target.write_text(generated, encoding="utf-8")
    print("openapi.json 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
