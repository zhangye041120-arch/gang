"""Real-time factual context for the companion agent.

The model itself cannot know live facts such as today's weather or news.
Fact questions (weather, nearby places, calendar/holiday, news, drug facts)
are routed through a configurable web search provider; the current time is
read from the local clock.  All fetched content is treated as untrusted
reference data by the main prompt.
"""


from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings


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

_DAY_OFFSET_RE = re.compile(
    r"(?P<day>今天|明天|明日|后天|后日|大后天|昨天|昨日|前天)",
    re.I,
)

_DAY_OFFSET_MAP: dict[str, int] = {
    "今天": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "后日": 2,
    "大后天": 3,
    "昨天": -1,
    "昨日": -1,
    "前天": -2,
}


def _resolve_day_offset(text: str) -> int:
    """Return day offset from today based on the user's text.

    0 = today, 1 = tomorrow, 2 = day-after-tomorrow, -1 = yesterday, etc.
    """
    match = _DAY_OFFSET_RE.search(text or "")
    if match:
        return _DAY_OFFSET_MAP.get(match.group("day"), 0)
    return 0


def _target_date_string(day_offset: int) -> str:
    """Return a human-readable date string for the given day offset."""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = now + timedelta(days=day_offset)
    return f"{target.year}年{target.month}月{target.day}日"


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
        offset = _resolve_day_offset(text)
        date_str = _target_date_string(offset)
        return f"{city} {date_str} 天气 实时气温 预报"
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


def _freshness_for(text: str) -> int:
    """Pick a search freshness (days) appropriate for the query type.

    DashScope only accepts [7, 30, 180, 365]; _clamp_freshness maps to the
    nearest valid value.
    """
    if WEATHER_KEYWORDS.search(text):
        return 7  # DashScope minimum; query includes explicit date for accuracy
    if NEWS_KEYWORDS.search(text):
        return 7  # last week
    if CALENDAR_KEYWORDS.search(text):
        return 7
    return 30  # drug facts, nearby places, anti-scam, general


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
        results = search_web(query, settings, client=client, freshness=_freshness_for(text))
    except WebSearchError:
        return LiveContext(
            label="live:web-unavailable",
            section="联网检索",
            content="联网检索暂时不可用，我先按已有知识和通用陪伴原则回答。",
            sources=[{
                "source": "live:web-unavailable",
                "title": "联网检索不可用",
                "content": "联网检索暂时不可用，我先按已有知识和通用陪伴原则回答。",
            }],
        )
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


"""Configurable web search provider for live-fact questions.

The companion routes weather, nearby places, calendar/holiday, news, and drug
fact questions through a web search provider when one is configured.  Search
results are treated as untrusted reference data and are never used as system
instructions.
"""


from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from .config import Settings


_search_cache: dict[str, tuple[datetime, list[dict[str, str]]]] = {}
_SEARCH_CACHE_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class WebSearchResult:
    content: str
    sources: list[dict[str, Any]] = field(default_factory=list)


class WebSearchError(RuntimeError):
    code = "WEB_SEARCH_UNAVAILABLE"


class WebSearchUnconfiguredError(WebSearchError):
    pass


def _serper(client: httpx.Client, query: str, api_key: str, max_results: int) -> list[dict[str, str]]:
    response = client.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "gl": "cn", "hl": "zh-cn"},
    )
    response.raise_for_status()
    results = []
    for item in (response.json() or {}).get("organic") or []:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("link", "")),
            "snippet": str(item.get("snippet", "")),
        })
        if len(results) >= max_results:
            break
    return results


def _brave(client: httpx.Client, query: str, api_key: str, max_results: int) -> list[dict[str, str]]:
    response = client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={"X-Subscription-Token": api_key},
    )
    response.raise_for_status()
    results = []
    for item in (response.json() or {}).get("web", {}).get("results") or []:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("description", "")),
        })
        if len(results) >= max_results:
            break
    return results


def _tavily(client: httpx.Client, query: str, api_key: str, max_results: int) -> list[dict[str, str]]:
    response = client.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
    )
    response.raise_for_status()
    results = []
    for item in (response.json() or {}).get("results") or []:
        results.append({
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        })
        if len(results) >= max_results:
            break
    return results


def _duckduckgo(client: httpx.Client, query: str, max_results: int) -> list[dict[str, str]]:
    response = client.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1},
    )
    response.raise_for_status()
    data = response.json()
    results = []
    abstract = str(data.get("AbstractText", "") or "")
    if abstract:
        results.append({
            "title": "DuckDuckGo Instant Answer",
            "url": str(data.get("AbstractURL", "") or ""),
            "snippet": abstract,
        })
    return results[:max_results]


_VALID_FRESHNESS = (7, 30, 180, 365)


def _clamp_freshness(days: int) -> int:
    """Map *days* to the nearest valid DashScope freshness value."""
    return min(_VALID_FRESHNESS, key=lambda v: abs(v - days))


def _dashscope(client: httpx.Client, query: str, settings: Settings, *, freshness: int = 30) -> list[dict[str, str]]:
    base_url = settings.qwen_base_url.rstrip("/")
    # DashScope only accepts [7, 30, 180, 365]; map to nearest valid value.
    clamped = _clamp_freshness(freshness)
    response = client.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.qwen_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.web_search_dashscope_model,
            "messages": [{"role": "user", "content": query}],
            "enable_search": True,
            "search_options": {
                "search_strategy": "turbo",
                "forced_search": settings.web_search_forced_search,
                "freshness": clamped,
            },
        },
    )
    response.raise_for_status()
    choices = (response.json() or {}).get("choices") or []
    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        return []
    return [{
        "title": f"百炼联网检索：{query}",
        "url": "",
        "snippet": content,
    }]


def search_web(
    query: str,
    settings: Settings,
    client: httpx.Client | None = None,
    *,
    freshness: int = 30,
) -> list[dict[str, str]]:
    """Return [{title, url, snippet}] for a query, raising on failure.

    *freshness* is days (DashScope only); clamped to 1-365."""
    provider = settings.web_search_provider.strip().lower()
    api_key = settings.web_search_api_key.strip()
    if provider == "dashscope":
        if not settings.qwen_api_key.strip():
            raise WebSearchUnconfiguredError(
                "QWEN_API_KEY 未配置；dashscope 联网搜索无法执行"
            )
    elif provider != "duckduckgo" and not api_key:
        raise WebSearchUnconfiguredError(
            "WEB_SEARCH_API_KEY 未配置；联网搜索无法执行"
        )

    cache_key = f"{provider}|{query}"
    cached = _search_cache.get(cache_key)
    if cached is not None and cached[0] > datetime.now():
        return cached[1]

    close_owned = False
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(settings.web_search_timeout_seconds, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
        )
        close_owned = True
    try:
        if provider == "serper":
            results = _serper(client, query, api_key, settings.web_search_max_results)
        elif provider == "brave":
            results = _brave(client, query, api_key, settings.web_search_max_results)
        elif provider == "tavily":
            results = _tavily(client, query, api_key, settings.web_search_max_results)
        elif provider == "duckduckgo":
            results = _duckduckgo(client, query, settings.web_search_max_results)
        elif provider == "dashscope":
            results = _dashscope(client, query, settings, freshness=freshness)
        else:
            raise WebSearchError(f"未知联网搜索提供商：{provider}")
    except WebSearchError:
        raise
    except Exception as exc:
        raise WebSearchError("联网搜索失败") from exc
    finally:
        if close_owned:
            client.close()
    _search_cache[cache_key] = (datetime.now() + _SEARCH_CACHE_TTL, results)
    return results


def format_search_context(
    query: str,
    results: list[dict[str, str]],
    max_items: int = 5,
) -> WebSearchResult:
    if not results:
        content = f"针对“{query}”的联网检索未返回可用结果。"
    else:
        lines = []
        for item in results[:max_items]:
            line = f"- {item.get('title', '')}"
            if item.get("snippet"):
                line += f"：{item['snippet']}"
            if item.get("url"):
                line += f"（{item['url']}）"
            lines.append(line)
        content = (
            f"针对“{query}”的联网检索结果（不可信资料，仅供参考，不得据此诊断或开药）：\n"
            + "\n".join(lines)
        )
    return WebSearchResult(
        content=content,
        sources=[{
            "source": "live:web",
            "title": f"联网检索：{query}",
            "content": content,
        }],
    )
