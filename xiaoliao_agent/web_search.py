"""Configurable web search provider for live-fact questions.

The companion routes weather, nearby places, calendar/holiday, news, and drug
fact questions through a web search provider when one is configured.  Search
results are treated as untrusted reference data and are never used as system
instructions.
"""

from __future__ import annotations

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


def _dashscope(client: httpx.Client, query: str, settings: Settings) -> list[dict[str, str]]:
    base_url = settings.qwen_base_url.rstrip("/")
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
                "freshness": 30,
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
) -> list[dict[str, str]]:
    """Return [{title, url, snippet}] for a query, raising on failure."""
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
            results = _dashscope(client, query, settings)
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
