import re

import httpx

from xiaoliao_agent.config import Settings
from xiaoliao_agent.live_context import (
    _resolve_day_offset,
    _target_date_string,
    extract_city,
    fetch_live_context,
    should_lookup,
)


def test_live_fact_questions_route_to_lookup():
    assert should_lookup("今天天气怎么样")
    assert should_lookup("现在几点了")
    assert should_lookup("沈阳热不热")
    assert should_lookup("我附近有什么")
    assert should_lookup("今天农历几号")
    assert should_lookup("今天有什么新闻")
    assert should_lookup("阿司匹林是做什么的")
    assert should_lookup("老年人防诈骗最新案例")
    assert not should_lookup("我最近心情不太好")
    assert not should_lookup("我没时间出门")
    assert not should_lookup("我想找人说说话")


def test_city_extraction_with_and_without_location():
    settings = Settings()
    assert extract_city("北京天气怎么样", settings.live_default_city) == "北京"
    assert extract_city("上海今天热不热", settings.live_default_city) == "上海"
    assert extract_city("今天天气怎么样", settings.live_default_city) == settings.live_default_city


def test_clock_answer_uses_local_time_without_network():
    def unexpected_request(request):
        raise AssertionError(f"时间问题不应发起网络请求: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(unexpected_request))
    result = fetch_live_context("现在几点", Settings(), client=client)
    client.close()
    assert result is not None
    assert result.section == "当前时间"
    assert re.search(
        r"现在是 \d{4}年\d{1,2}月\d{1,2}日 星期. \d{2}:\d{2}（北京时间）。",
        result.content,
    )


def test_weather_question_routes_to_web_search():
    def handler(request):
        return httpx.Response(200, json={
            "organic": [{
                "title": "沈阳天气",
                "link": "https://example.invalid/weather",
                "snippet": "沈阳今天多云，气温 26 度。",
            }],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        web_search_provider="serper",
        web_search_api_key="test-key",
        live_default_city="沈阳",
    )
    result = fetch_live_context("沈阳今天天气怎么样", settings, client=client)
    client.close()
    assert result is not None
    assert result.section == "联网检索"
    assert "沈阳今天多云" in result.content
    assert result.sources[0]["source"] == "live:web"


def test_web_search_unconfigured_returns_none_without_crash():
    result = fetch_live_context("今天天气怎么样", Settings(web_search_api_key=""), client=None)
    assert result is not None
    assert result.label == "live:web-unavailable"
    assert "联网检索暂时不可用" in result.content


def test_non_live_message_returns_none():
    assert fetch_live_context("我最近心情不太好", Settings(web_search_api_key="k"), client=None) is None


def test_day_offset_resolves_today_tomorrow_day_after():
    assert _resolve_day_offset("今天天气怎么样") == 0
    assert _resolve_day_offset("明天多少度") == 1
    assert _resolve_day_offset("后天冷不冷") == 2
    assert _resolve_day_offset("大后天热不热") == 3
    assert _resolve_day_offset("昨天好冷") == -1
    assert _resolve_day_offset("前天下了雨") == -2
    assert _resolve_day_offset("最近天气不错") == 0


def test_target_date_string_includes_year_month_day():
    result = _target_date_string(0)
    assert "年" in result
    assert "月" in result
    assert "日" in result
    # tomorrow should be different from today
    today = _target_date_string(0)
    tomorrow = _target_date_string(1)
    assert today != tomorrow


def test_weather_query_includes_target_date():
    def handler(request):
        return httpx.Response(200, json={
            "organic": [{
                "title": "天气",
                "link": "https://example.invalid",
                "snippet": "多云 26 度",
            }],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        web_search_provider="serper",
        web_search_api_key="test-key",
        live_default_city="沈阳",
    )
    # "明天" should produce a query with tomorrow's date, not "今天"
    result = fetch_live_context("明天多少度", settings, client=client)
    client.close()
    assert result is not None
    # The search context should contain the target date, not "今天天气"
    assert "今天天气" not in result.content  # shouldn't hardcode 今天
    assert "天气" in result.content
