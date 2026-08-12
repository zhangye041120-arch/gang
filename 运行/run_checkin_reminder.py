import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiaoliao_agent.reminders import (
    DailyCheckinService,
    checkin_policy_from_settings,
)
from xiaoliao_agent.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="小辽企微员工群每日签到提醒")
    parser.add_argument("--once", action="store_true", help="只检查一次到点提醒后退出")
    parser.add_argument("--loop", action="store_true", help="每 60 秒循环检查")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.wecom_checkin_reminder_enabled:
        print("签到提醒未开启：WECOM_CHECKIN_REMINDER_ENABLED=false")
        return 2
    if not settings.wecom_checkin_group_webhook and not settings.wecom_checkin_group_user_schedule_path:
        print("未配置 WECOM_CHECKIN_GROUP_WEBHOOK 或 WECOM_CHECKIN_GROUP_USER_SCHEDULE_PATH")
        return 2
    if settings.wecom_checkin_group_user_schedule_path and not (
        settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_agent_secret
    ):
        print("用户级私信需要 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_AGENT_SECRET")
        return 2
    service = DailyCheckinService(checkin_policy_from_settings(settings))

    if args.once:
        print(json.dumps(service.run_due(), ensure_ascii=False, indent=2))
        return 0

    print(f"签到提醒循环已启动，计划时间：{settings.wecom_checkin_group_times}")
    while True:
        try:
            events = service.run_due()
            for event in events:
                if event.get("status") in {"sent", "failed"}:
                    print(json.dumps(event, ensure_ascii=False))
            time.sleep(60)
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
