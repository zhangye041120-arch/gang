import json

import pytest

from xiaoliao_agent.embeddings import EmbeddingError, OpenAICompatibleEmbeddingClient


class FakeResponse:
    headers = {}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


def test_embedding_response_is_ordered_and_dimension_checked(monkeypatch):
    monkeypatch.setattr(
        "xiaoliao_agent.embeddings.urlopen",
        lambda *args, **kwargs: FakeResponse({"data": [
            {"index": 1, "embedding": [0.2, 0.3]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ]}),
    )
    client = OpenAICompatibleEmbeddingClient("https://embedding.invalid/v1", "key", "model", dimension=2)
    assert client.embed(["a", "b"]) == [[0.1, 0.2], [0.2, 0.3]]


def test_embedding_dimension_mismatch_fails_before_database_write(monkeypatch):
    monkeypatch.setattr(
        "xiaoliao_agent.embeddings.urlopen",
        lambda *args, **kwargs: FakeResponse({"data": [{"index": 0, "embedding": [0.1]}]}),
    )
    client = OpenAICompatibleEmbeddingClient("https://embedding.invalid/v1", "key", "model", dimension=2)
    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        client.embed(["a"])
