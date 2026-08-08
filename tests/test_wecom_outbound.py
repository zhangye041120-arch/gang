import base64
import hashlib
import hmac
import json

import httpx
import pytest

from xiaoliao_agent.wecom_outbound import (
    WeComAppMessageError,
    WeComAppMessageSender,
    WeComWebhookError,
    WeComWebhookSender,
    append_webhook_signature,
    truncate_utf8_bytes,
    wecom_webhook_signature,
)


def test_webhook_signature_matches_hmac_sha256_base64():
    ts, sign = wecom_webhook_signature("secret-value", timestamp=1700000000)
    message = f"1700000000\nsecret-value".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(b"secret-value", message, hashlib.sha256).digest()
    ).decode("ascii")
    assert sign == expected
    assert ts == 1700000000


def test_append_webhook_signature_preserves_existing_query():
    url = append_webhook_signature(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        "secret",
        timestamp=1700000000,
    )
    assert url.startswith(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc&timestamp=1700000000&sign="
    )
    assert append_webhook_signature("https://x", "") == "https://x"


def test_send_text_posts_expected_payload_and_mentions():
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    sender = WeComWebhookSender(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = sender.send_text(
        "今天的签到提醒",
        mentioned_user_ids=["zhangsan"],
        mentioned_mobiles=["13800000000"],
        mention_all=True,
    )
    assert result["errcode"] == 0
    payload = captured["payload"]
    assert payload["msgtype"] == "text"
    assert payload["text"]["content"] == "今天的签到提醒"
    assert payload["text"]["mentioned_list"] == ["zhangsan", "@all"]
    assert payload["text"]["mentioned_mobile_list"] == ["13800000000"]


def test_send_text_raises_on_wecom_error():
    def handler(request):
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    sender = WeComWebhookSender(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(WeComWebhookError, match="93000"):
        sender.send_text("x")


def test_send_text_requires_webhook_url():
    sender = WeComWebhookSender("")
    with pytest.raises(WeComWebhookError, match="webhook_url"):
        sender.send_text("x")


def test_truncate_utf8_bytes_keeps_character_boundary():
    assert truncate_utf8_bytes("a" * 3000, max_bytes=10) == "a" * 10
    assert truncate_utf8_bytes("好" * 5, max_bytes=5) == "好"


def test_app_message_sender_fetches_token_and_sends_private_text():
    requests = []

    def handler(request):
        requests.append((request.method, str(request.url), request.read().decode("utf-8")))
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={
                "errcode": 0,
                "errmsg": "ok",
                "access_token": "TOKEN",
                "expires_in": 7200,
            })
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    sender = WeComAppMessageSender(
        "corp-id",
        1000002,
        "agent-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = sender.send_text("zhangsan", "该签到啦")
    assert result["errcode"] == 0
    assert requests[0][0] == "GET"
    assert "corpid=corp-id" in requests[0][1]
    payload = json.loads(requests[1][2])
    assert payload == {
        "touser": "zhangsan",
        "msgtype": "text",
        "agentid": 1000002,
        "text": {"content": "该签到啦"},
    }


def test_app_message_sender_raises_on_wecom_error():
    def handler(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={
                "errcode": 0,
                "errmsg": "ok",
                "access_token": "TOKEN",
                "expires_in": 7200,
            })
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "not allowed"})

    sender = WeComAppMessageSender(
        "corp-id",
        1000002,
        "agent-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(WeComAppMessageError, match="60020"):
        sender.send_text("zhangsan", "x")


def test_app_message_sender_retries_once_after_invalid_token():
    send_count = {"value": 0}

    def handler(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={
                "errcode": 0,
                "errmsg": "ok",
                "access_token": "TOKEN",
                "expires_in": 7200,
            })
        send_count["value"] += 1
        if send_count["value"] == 1:
            return httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid token"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    sender = WeComAppMessageSender(
        "corp-id",
        1000002,
        "agent-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert sender.send_text("zhangsan", "x")["errcode"] == 0
    assert send_count["value"] == 2


def test_app_message_sender_requires_credentials():
    sender = WeComAppMessageSender("", 0, "")
    with pytest.raises(WeComAppMessageError, match="credentials"):
        sender.send_text("zhangsan", "x")
