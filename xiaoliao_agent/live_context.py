"""Real-time factual context for the companion agent.

The model itself cannot know live facts such as today's weather or news.
Fact questions (weather, nearby places, calendar/holiday, news, drug facts)
are routed through a configurable web search provider; the current time is
read from the local clock.  All fetched content is treated as untrusted
reference data by the main prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .web_search import WebSearchError, format_search_context, search_web


WEATHER_KEYWORDS = re.compile(
    r"(天气|气温|温度|多少度|热不热|冷不冷|下不下雨|会不会下雨|"
    r"会不会下雪|天气预报|带不带伞|要不要带伞|刮风|下雨|下雪|晴天|阴天|降温|升温)",
    re.I,
)

TIME_KEYWORDS = re.compile(
    r"(几点|几点了|几号|星期几|周几|礼拜几|日期|哪年|多少年|年份|"
    r"现在.{0,4}时间|今天.{0,4}(几号|星期|日期)|今年.{0,4}(年|年份))",
    re.I,
)

NEARBY_KEYWORDS = re.compile(
    r"(附近|周边|附近有什么|周边有什么|周边设施|哪里可以|哪里有|哪儿有|去哪儿|"
    r"离我近|离得近)",
    re.I,
)

CALENDAR_KEYWORDS = re.compile(
    r"(农历|阴历|老历|属相|生肖|节气|二十四节气|放假|假期|节假日|法定假日|"
    r"春节|端午节|中秋节|国庆节|元旦|清明|过年|过生日|几号生日)",
    re.I,
)

NEWS_KEYWORDS = re.compile(r"(新闻|简讯|今天有什么新闻|最近有什么新闻|时事|要闻|头条)", re.I)

DRUG_KEYWORDS = re.compile(
    r"(药品|药是做什么|药有什么用|说明书|禁忌|副作用|阿司匹林|感冒药|降压药|降糖药)",
    re.I,
)

SCAM_CASE_KEYWORDS = re.compile(r"(防诈骗|防骗|诈骗|骗局|骗子|案例)", re.I)

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_CITY_PATTERN = re.compile(
    r"([\u4e00-\u9fa5]{2,6}?)(?:市|县|区|省)?的?(?:今天|明天|后天|现在|这几天|外面|这边|咱们这|我们这)?"
    r"(?:天气|气温|温度|多少度|热不热|冷不冷|下不下雨|天气预报)"
)

_CITY_FILLER = re.compile(r"^(今天|明天|后天|现在|这几天|外面|这边|咱们这|我们这)")


@dataclass(frozen=True)
class LiveContext:
    label: str
    content: str
    section: str = "实时信息"
    sources: list[dict[str, Any]] = field(default_factory=list)


def should_lookup(text: str) -> bool:
    return bool(
        WEATHER_KEYWORDS.search(text or "")
        or TIME_KEYWORDS.search(text or "")
        or NEARBY_KEYWORDS.search(text or "")
        or CALENDAR_KEYWORDS.search(text or "")
        or NEWS_KEYWORDS.search(text or "")
        or DRUG_KEYWORDS.search(text or "")
        or SCAM_CASE_KEYWORDS.search(text or "")
    )


def extract_city(text: str, default_city: str) -> str:
    match = _CITY_PATTERN.search(text or "")
    if not match:
        return default_city
    city = match.group(1).strip()
    city = _CITY_FILLER.sub("", city)
    return city or default_city


def _format_clock(now: datetime) -> str:
    return (
        f"现在是 {now.year}年{now.month}月{now.day}日 "
        f"{_WEEKDAYS[now.weekday()]} {now.hour:02d}:{now.minute:02d}（北京时间）。"
    )


def _fetch_clock() -> LiveContext:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    content = _format_clock(now)
    return LiveContext(
        label="live:clock",
        section="当前时间",
        content=content,
        sources=[{
            "source": "live:clock",
            "title": "当前时间",
            "content": content,
        }],
    )


def _web_search_query(text: str, settings: Settings) -> str | None:
    if WEATHER_KEYWORDS.search(text):
        city = extract_city(text, settings.live_default_city)
        return f"{city} 今天天气 气温 穿衣建议"
    if NEARBY_KEYWORDS.search(text):
        city = extract_city(text, settings.live_default_city)
        return f"{city} 附近 医院 药店 公园 超市 菜市场"
    if CALENDAR_KEYWORDS.search(text):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return f"今天是{now.year}年{now.month}月{now.day}日 农历 节气 法定节假日"
    if NEWS_KEYWORDS.search(text):
        return "今日 民生新闻 简讯"
    if DRUG_KEYWORDS.search(text):
        return "药品 通用科普 说明书 禁忌 注意事项"
    if SCAM_CASE_KEYWORDS.search(text):
        return "老年人 防诈骗 最新案例 2026"
    return None


def _fetch_web_context(
    text: str,
    settings: Settings,
    client: httpx.Client | None,
) -> LiveContext | None:
    if not settings.web_search_enabled:
        return None
    query = _web_search_query(text, settings)
    if not query:
        return None
    try:
        results = search_web(query, settings, client=client)
    except WebSearchError:
        return None
    search_result = format_search_context(query, results, settings.web_search_max_results)
    return LiveContext(
        label="live:web",
        section="联网检索",
        content=search_result.content,
        sources=search_result.sources,
    )


def fetch_live_context(
    text: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> LiveContext | None:
    """Return live context for time or web-searchable fact questions."""
    if not settings.live_lookup_enabled:
        return None
    if not should_lookup(text):
        return None
    if CALENDAR_KEYWORDS.search(text or ""):
        return _fetch_web_context(text, settings, client)
    if TIME_KEYWORDS.search(text or ""):
        return _fetch_clock()
    return _fetch_web_context(text, settings, client)
