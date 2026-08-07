import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


class EmbeddingError(RuntimeError):
    pass


class OpenAICompatibleEmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, *, dimension: int, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        request_id = uuid.uuid4().hex
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(
                {"model": self.model, "input": texts, "dimensions": self.dimension},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise EmbeddingError(f"embedding HTTP {exc.code} request_id={request_id}") from exc
        except URLError as exc:
            raise EmbeddingError(f"embedding network unavailable request_id={request_id}") from exc
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EmbeddingError(f"embedding response invalid request_id={request_id}") from exc
        try:
            rows = body["data"]
            ordered = sorted(rows, key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding response contract invalid request_id={request_id}") from exc
        if len(vectors) != len(texts) or any(len(vector) != self.dimension for vector in vectors):
            actual = len(vectors[0]) if vectors else 0
            raise EmbeddingError(
                f"embedding dimension mismatch expected={self.dimension} actual={actual} request_id={request_id}"
            )
        return vectors
