import json

import httpx
import pytest

from xiaoliao_agent.config import Settings
from xiaoliao_agent.live_context import (
    WebSearchError,
    WebSearchUnconfiguredError,
    format_search_context,
    search_web,
)


def test_serper_results_are_parsed():
    def handler(request):
        assert request.headers["X-API-KEY"] == "serper-key"
        return httpx.Response(200, json={
            "organic": [
                {"title": "沈阳天气", "link": "https://a.invalid/1", "snippet": "多云 26 度"},
                {"title": "沈阳生活", "link": "https://a.invalid/2", "snippet": "便民信息"},
            ],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_web("沈阳天气", Settings(web_search_provider="serper", web_search_api_key="serper-key"), client=client)
    client.close()
    assert results == [
        {"title": "沈阳天气", "url": "https://a.invalid/1", "snippet": "多云 26 度"},
        {"title": "沈阳生活", "url": "https://a.invalid/2", "snippet": "便民信息"},
    ]


def test_brave_results_are_parsed():
    def handler(request):
        assert request.headers["X-Subscription-Token"] == "brave-key"
        return httpx.Response(200, json={
            "web": {"results": [
                {"title": "新闻简讯", "url": "https://b.invalid/n", "description": "民生新闻"},
            ]},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_web("今日新闻", Settings(web_search_provider="brave", web_search_api_key="brave-key"), client=client)
    client.close()
    assert results[0]["title"] == "新闻简讯"


def test_tavily_results_are_parsed():
    def handler(request):
        return httpx.Response(200, json={
            "results": [{"title": "药品说明", "url": "https://t.invalid/d", "content": "通用常识"}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_web("药品科普", Settings(web_search_provider="tavily", web_search_api_key="tavily-key"), client=client)
    client.close()
    assert results[0]["title"] == "药品说明"


def test_missing_api_key_is_reported_as_unconfigured():
    with pytest.raises(WebSearchUnconfiguredError):
        search_web("天气", Settings(web_search_provider="serper", web_search_api_key=""))


def test_network_failure_is_reported_as_web_search_error():
    def failing(request):
        raise httpx.ConnectError("offline")

    client = httpx.Client(transport=httpx.MockTransport(failing))
    with pytest.raises(WebSearchError):
        search_web("天气", Settings(web_search_provider="serper", web_search_api_key="k"), client=client)
    client.close()


def test_dashscope_web_search_uses_qwen_key_and_parses_answer():
    def handler(request):
        body = json.loads(request.content)
        assert body["enable_search"] is True
        assert body["search_options"]["forced_search"] is False
        assert body["model"] == "qwen3.7-flash"
        # freshness=7 maps to valid DashScope value 7
        assert body["search_options"]["freshness"] == 7
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "沈阳今天多云，气温26度。"}}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        web_search_provider="dashscope",
        qwen_api_key="dash-key",
        qwen_base_url="https://dashscope.invalid/v1",
    )
    results = search_web("沈阳天气", settings, client=client, freshness=7)
    client.close()
    assert results[0]["title"].startswith("百炼联网检索")
    assert "沈阳今天多云" in results[0]["snippet"]


def test_dashscope_defaults_to_30_day_freshness():
    def handler(request):
        body = json.loads(request.content)
        assert body["search_options"]["freshness"] == 30
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "通用科普内容。"}}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        web_search_provider="dashscope",
        qwen_api_key="dash-key",
        qwen_base_url="https://dashscope.invalid/v1",
    )
    search_web("药品科普", settings, client=client)  # no freshness → default 30
    client.close()


def test_freshness_clamps_to_valid_dashscope_values():
    from xiaoliao_agent.live_context import _clamp_freshness
    assert _clamp_freshness(1) == 7   # nearest to 7
    assert _clamp_freshness(7) == 7
    assert _clamp_freshness(15) == 7  # |15-7|=8 < |15-30|=15
    assert _clamp_freshness(20) == 30  # |20-30|=10 < |20-7|=13
    assert _clamp_freshness(30) == 30
    assert _clamp_freshness(150) == 180  # |150-180|=30 < |150-30|=120
    assert _clamp_freshness(365) == 365


def test_dashscope_without_qwen_key_is_unconfigured():
    with pytest.raises(WebSearchUnconfiguredError):
        search_web("天气", Settings(web_search_provider="dashscope", qwen_api_key=""))


def test_format_search_context_marks_results_as_untrusted():
    result = format_search_context("沈阳天气", [{"title": "天气", "url": "https://x", "snippet": "多云"}])
    assert "不可信资料" in result.content
    assert "不得据此诊断或开药" in result.content
    assert result.sources[0]["source"] == "live:web"


def test_results_are_cached_within_ttl():
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(200, json={
            "organic": [{"title": "t", "link": "https://c.invalid", "snippet": "s"}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(web_search_provider="serper", web_search_api_key="k")
    query = "缓存测试唯一查询-20260807"
    search_web(query, settings, client=client)
    search_web(query, settings, client=client)
    client.close()
    assert calls["count"] == 1
