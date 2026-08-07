import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiaoliao_agent import Settings, XiaoliaoAgent


def main() -> int:
    parser = argparse.ArgumentParser(description="小辽智能体交互式命令行")
    parser.add_argument("--message", help="只发送一条消息后退出")
    parser.add_argument("--stream", action="store_true", help="流式逐段显示回复（安全审核通过后输出）")
    parser.add_argument("--debug", action="store_true", help="显示检索来源与副 Agent 检验信息")
    args = parser.parse_args()

    try:
        agent = XiaoliaoAgent(Settings.from_env())
    except RuntimeError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    history: list[dict[str, str]] = []

    def stream_reply(text: str) -> str:
        pieces: list[str] = []
        done: dict[str, object] = {}
        for event in agent.chat_stream(text, history):
            if event.get("type") == "token":
                pieces.append(event.get("content", ""))
                print(event.get("content", ""), end="", flush=True)
            elif event.get("type") == "done":
                done = event
        print()
        if done.get("action"):
            action = done["action"]
            print(f"[行动建议] {action.get('module')} -> {action.get('page')}")
        if args.debug and done:
            print(json.dumps({
                "intent": done.get("intent"),
                "blocked": done.get("blocked"),
                "rewritten": done.get("rewritten"),
                "latency_ms": done.get("latency_ms"),
                "error_code": done.get("error_code"),
            }, ensure_ascii=False, indent=2))
        return "".join(pieces)

    def ask(text: str) -> None:
        if args.stream:
            reply = stream_reply(text)
        else:
            result = agent.chat(text, history)
            print(f"小辽：{result.reply}")
            if result.action:
                print(f"[行动建议] {result.action.get('module')} -> {result.action.get('page')}")
            if args.debug:
                print(json.dumps({
                    "intent": result.intent,
                    "blocked": result.blocked,
                    "rewritten": result.rewritten,
                    "inspection": result.to_dict()["inspection"],
                    "sources": result.sources,
                }, ensure_ascii=False, indent=2))
            reply = result.reply
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])

    if args.message:
        ask(args.message)
        return 0

    print("小辽智能体已启动。输入 /exit 退出，/clear 清空本轮上下文。")
    print(f"当前模式：在线 DeepSeek ({agent.settings.deepseek_model}) + 在线 Qwen ({agent.settings.qwen_model})")
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if not text:
            continue
        if text.lower() in {"/exit", "/quit", "退出"}:
            print("小辽：今天先聊到这里，慢慢来就好。")
            return 0
        if text.lower() == "/clear":
            history.clear()
            print("[已清空本轮上下文]")
            continue
        ask(text)


if __name__ == "__main__":
    raise SystemExit(main())
