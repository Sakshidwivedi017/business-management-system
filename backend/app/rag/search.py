"""Semantic search over the knowledge index: one query embedding, one bounded vector search."""

from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission
from app.config import get_settings
from app.db.repositories import knowledge as repo
from app.observability import traced_step
from app.rag.documents import SOURCES
from app.rag.embeddings import get_embedder
from app.services.errors import ValidationError, database_errors
from app.services.validation import require_text

MAX_QUERY_LENGTH = 500
DEFAULT_TOP_K = 5
MAX_TOP_K = 10
NO_RESULTS_MESSAGE = "No sufficiently relevant knowledge was found."


@database_errors("search_knowledge")
def search_knowledge(
    conn: Connection,
    user: AuthenticatedUser,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source: str | None = None,
) -> dict[str, Any]:
    """Most similar indexed documents above the configured similarity threshold, best first."""
    # Every indexed source is inventory catalogue data, so it is readable exactly where inventory is.
    ensure_permission(user, Permission.INVENTORY_READ)
    query = require_text(query, "query", MAX_QUERY_LENGTH)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise ValidationError(f"top_k must be an integer between 1 and {MAX_TOP_K}")
    if source is not None and source not in SOURCES:
        raise ValidationError(f"source must be one of: {', '.join(SOURCES)}")

    threshold = get_settings().rag_similarity_threshold
    # Layer 16: counts and similarity range only; never the query, vectors or documents.
    with traced_step("semantic_retrieval", "retriever", source=source, top_k=top_k,
                     min_similarity=threshold) as span:
        embedder = get_embedder()
        [vector] = embedder.embed([query])
        rows = repo.search(
            conn, vector, embedding_model=embedder.model, min_similarity=threshold, limit=top_k, source=source
        )
        similarities = [round(float(row["similarity"]), 3) for row in rows]
        span.update(results=len(rows), similarity_max=max(similarities, default=None),
                    similarity_min=min(similarities, default=None))
    result: dict[str, Any] = {
        "results": [
            {
                "source": row["source"],
                "source_id": row["source_id"],
                "title": row["title"],
                "similarity": round(float(row["similarity"]), 3),
                "content": row["content"],
                "metadata": row["metadata"],
            }
            for row in rows
        ],
        "min_similarity": threshold,
    }
    if not rows:
        result["message"] = NO_RESULTS_MESSAGE
    return result
