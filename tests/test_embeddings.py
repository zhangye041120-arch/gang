import json

import pytest

from xiaoliao_agent.providers import (
    DashScopeRerankClient,
    EmbeddingError,
    OpenAICompatibleEmbeddingClient,
    RerankError,
)


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
        "xiaoliao_agent.providers.urlopen",
        lambda *args, **kwargs: FakeResponse({"data": [
            {"index": 1, "embedding": [0.2, 0.3]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ]}),
    )
    client = OpenAICompatibleEmbeddingClient("https://embedding.invalid/v1", "key", "model", dimension=2)
    assert client.embed(["a", "b"]) == [[0.1, 0.2], [0.2, 0.3]]


def test_embedding_dimension_mismatch_fails_before_database_write(monkeypatch):
    monkeypatch.setattr(
        "xiaoliao_agent.providers.urlopen",
        lambda *args, **kwargs: FakeResponse({"data": [{"index": 0, "embedding": [0.1]}]}),
    )
    client = OpenAICompatibleEmbeddingClient("https://embedding.invalid/v1", "key", "model", dimension=2)
    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        client.embed(["a"])


def test_rerank_returns_scores_in_input_order(monkeypatch):
    monkeypatch.setattr(
        "xiaoliao_agent.providers.urlopen",
        lambda *args, **kwargs: FakeResponse({"output": {"results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
            {"index": 1, "relevance_score": 0.7},
        ]}}),
    )
    client = DashScopeRerankClient("key", timeout=5)
    assert client.rerank("query", ["a", "b", "c"]) == [0.8, 0.7, 0.9]


def test_rerank_rejects_incomplete_contract(monkeypatch):
    monkeypatch.setattr(
        "xiaoliao_agent.providers.urlopen",
        lambda *args, **kwargs: FakeResponse({"output": {"results": [
            {"index": 0, "relevance_score": 0.5},
        ]}}),
    )
    client = DashScopeRerankClient("key", timeout=5)
    with pytest.raises(RerankError, match="contract invalid"):
        client.rerank("query", ["a", "b"])
