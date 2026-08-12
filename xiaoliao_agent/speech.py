"""Text-to-speech via the DashScope speech synthesis API."""

import json
from typing import Any

import httpx


class TTSClientError(RuntimeError):
    pass


class DashScopeTTSClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "qwen-audio-3.0-tts-flash",
        voice: str = "longanhuan_v3.6",
        audio_format: str = "wav",
        sample_rate: int = 24000,
        base_url: str = "https://dashscope.aliyuncs.com/api/v1",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            raise TTSClientError("text is required")
        response = self._client.post(
            f"{self.base_url}/services/audio/tts/SpeechSynthesizer",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "input": {
                    "text": text,
                    "voice": self.voice,
                    "format": self.audio_format,
                    "sample_rate": self.sample_rate,
                },
            },
        )
        if response.status_code != 200:
            raise TTSClientError(f"tts HTTP {response.status_code}")
        try:
            body: dict[str, Any] = response.json()
            audio_url = body["output"]["audio"]["url"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TTSClientError("tts response contract invalid") from exc
        audio_response = self._client.get(audio_url)
        if audio_response.status_code != 200:
            raise TTSClientError(f"tts audio download failed: {audio_response.status_code}")
        return audio_response.content
