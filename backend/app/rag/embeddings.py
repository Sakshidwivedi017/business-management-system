"""OpenAI embeddings. Reuses OPENAI_API_KEY; the key is never logged or returned."""

import logging
from functools import lru_cache
from typing import get_args

from openai import OpenAI, OpenAIError
from openai.types import EmbeddingModel

from app.config import get_settings
from app.services.errors import BusinessError

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 30
# Models the installed openai package declares for the embeddings endpoint.
SUPPORTED_MODELS = frozenset(get_args(EmbeddingModel))


class EmbeddingError(BusinessError):
    """Embedding unavailable or misconfigured. The message is safe to show; details are only logged."""


class Embedder:
    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order, sent in batches. The SDK retries rate limits and 5xx itself."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            try:
                response = self.client.embeddings.create(model=self.model, input=batch)
            except OpenAIError as exc:
                logger.error("Embedding request failed: %s", type(exc).__name__)
                raise EmbeddingError("Semantic search is temporarily unavailable; please try again later") from None
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                logger.error("Embedding response had %d vectors for %d inputs", len(data), len(batch))
                raise EmbeddingError("Semantic search is temporarily unavailable; please try again later")
            vectors.extend(item.embedding for item in data)
        return vectors


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.openai_api_key is None or settings.embedding_model is None:
        logger.error("Semantic search needs OPENAI_API_KEY and EMBEDDING_MODEL in backend/.env")
        raise EmbeddingError("Semantic search is not configured")
    if settings.embedding_model not in SUPPORTED_MODELS:
        logger.error("EMBEDDING_MODEL is not one of %s", sorted(SUPPORTED_MODELS))
        raise EmbeddingError("Semantic search is not configured")
    client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value(), timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2
    )
    return Embedder(client, settings.embedding_model)
