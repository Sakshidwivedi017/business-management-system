"""OpenAI embedding client wrapper (the API is faked; no credits are used)."""

import logging
from types import SimpleNamespace

import pytest
from openai import OpenAIError
from pydantic import SecretStr

from app.config import get_settings
from app.rag import embeddings
from app.rag.embeddings import Embedder, EmbeddingError, get_embedder

FAKE_KEY = "sk-test-not-a-real-key-0123456789"


class FakeEmbeddingsAPI:
    def __init__(self, error=None, drop=0, shuffle=False):
        self.calls = []
        self.error, self.drop, self.shuffle = error, drop, shuffle
        self.embeddings = self

    def create(self, model, input):
        self.calls.append((model, list(input)))
        if self.error:
            raise self.error
        data = [SimpleNamespace(index=i, embedding=[float(len(t)), float(i)]) for i, t in enumerate(input)]
        data = data[: len(data) - self.drop]
        return SimpleNamespace(data=list(reversed(data)) if self.shuffle else data)


@pytest.fixture
def settings(monkeypatch):
    def use(**overrides):
        monkeypatch.setattr(embeddings, "get_settings", lambda: get_settings().model_copy(update=overrides))
        get_embedder.cache_clear()

    yield use
    get_embedder.cache_clear()


def test_batches_and_preserves_order():
    api = FakeEmbeddingsAPI(shuffle=True)
    texts = [f"text {i}" for i in range(250)]
    vectors = Embedder(api, "text-embedding-3-small").embed(texts)
    assert [len(batch) for _, batch in api.calls] == [100, 100, 50]
    assert {model for model, _ in api.calls} == {"text-embedding-3-small"}
    assert vectors == [[float(len(t)), float(i % 100)] for i, t in enumerate(texts)]


def test_empty_input_makes_no_request():
    api = FakeEmbeddingsAPI()
    assert Embedder(api, "m").embed([]) == [] and api.calls == []


def test_api_failure_is_safe(caplog):
    api = FakeEmbeddingsAPI(error=OpenAIError(f"Incorrect API key provided: {FAKE_KEY}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as raised:
        Embedder(api, "m").embed(["x"])
    assert str(raised.value) == "Semantic search is temporarily unavailable; please try again later"
    assert raised.value.__cause__ is None
    assert FAKE_KEY not in caplog.text


def test_incomplete_response_is_an_error():
    with pytest.raises(EmbeddingError):
        Embedder(FakeEmbeddingsAPI(drop=1), "m").embed(["a", "b"])


def test_configured_embedder(settings):
    settings(openai_api_key=SecretStr(FAKE_KEY), embedding_model="text-embedding-3-small")
    embedder = get_embedder()
    assert embedder.model == "text-embedding-3-small"
    assert get_embedder() is embedder  # one client per process
    assert FAKE_KEY not in repr(embedder.__dict__)


@pytest.mark.parametrize(
    "overrides",
    [
        {"openai_api_key": None, "embedding_model": "text-embedding-3-small"},
        {"openai_api_key": SecretStr(FAKE_KEY), "embedding_model": None},
        {"openai_api_key": SecretStr(FAKE_KEY), "embedding_model": "text-embedding-imaginary"},
    ],
)
def test_misconfiguration_refused(settings, overrides, caplog):
    settings(**overrides)
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError, match="^Semantic search is not configured$"):
        get_embedder()
    assert FAKE_KEY not in caplog.text


def test_supported_models_come_from_installed_package():
    assert "text-embedding-3-small" in embeddings.SUPPORTED_MODELS
