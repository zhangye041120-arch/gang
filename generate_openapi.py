import json
from pathlib import Path

from api_server import app


def main() -> int:
    target = Path(__file__).resolve().parent / "openapi.json"
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
    print("openapi.json 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
