from xiaoliao_agent.speech import DashScopeTTSClient, TTSClientError


class FakeResponse:
    status_code = 200

    def __init__(self, body=None, content=b""):
        self.body = body or {}
        self.content = content

    def json(self):
        return self.body


class FakeClient:
    def __init__(self):
        self.posted_url = None
        self.posted_body = None

    def post(self, url, *, headers, json):
        self.posted_url = url
        self.posted_body = json
        return FakeResponse(body={
            "output": {"audio": {"url": "https://audio.invalid/1.wav"}},
            "usage": {"characters": 2},
        })

    def get(self, url):
        return FakeResponse(content=b"fake-audio")


def test_tts_synthesizes_audio_from_dashscope():
    client = DashScopeTTSClient("key", client=FakeClient())
    audio = client.synthesize("你好")
    assert audio == b"fake-audio"


def test_tts_rejects_contract_violation():
    class BrokenClient:
        def post(self, url, *, headers, json):
            return FakeResponse(body={"output": {}})

    client = DashScopeTTSClient("key", client=BrokenClient())
    try:
        client.synthesize("你好")
    except TTSClientError as exc:
        assert "contract" in str(exc)
    else:
        raise AssertionError("expected TTSClientError")
