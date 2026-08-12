import argparse
import difflib

from xiaoliao_agent.prompts import get_prompt_spec


def main() -> int:
    parser = argparse.ArgumentParser(description="比较两个不可变 Prompt 版本")
    parser.add_argument("prompt_id", choices=["main-agent", "inspector", "rewrite"])
    parser.add_argument("old_version")
    parser.add_argument("new_version")
    args = parser.parse_args()
    old = get_prompt_spec(args.prompt_id, args.old_version)
    new = get_prompt_spec(args.prompt_id, args.new_version)
    print(f"{old.prompt_id}: {old.semantic_version} -> {new.semantic_version}")
    print(f"old_changelog={old.changelog}")
    print(f"new_changelog={new.changelog}")
    for line in difflib.unified_diff(
        old.content.splitlines(),
        new.content.splitlines(),
        fromfile=f"{args.prompt_id}@{args.old_version}",
        tofile=f"{args.prompt_id}@{args.new_version}",
        lineterm="",
    ):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
