"""Data access for semantic retrieval: source text from inventory tables and the rag.documents index.

The index lives in its own `rag` schema, together with the pgvector extension,
so the public business schema is untouched. Embeddings are passed as pgvector
text literals ("[0.1,0.2,...]"); the column has no fixed dimension, and every
query is restricted to one embedding model so dimensions always match.
Write functions run inside the caller's transaction and never commit.
"""

import json
from typing import Any

from sqlalchemy import Connection, text

from app.db.connection import fetch_all, fetch_one

# Additive and idempotent; run only by the explicit `python -m app.rag.index --create-schema`.
SCHEMA_DDL = (
    "CREATE SCHEMA IF NOT EXISTS rag",
    "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA rag",
    """
    CREATE TABLE IF NOT EXISTS rag.documents (
        source text NOT NULL,
        source_id text NOT NULL,
        title text NOT NULL,
        content text NOT NULL,
        metadata jsonb NOT NULL DEFAULT '{}',
        content_hash text NOT NULL,
        embedding_model text NOT NULL,
        embedding rag.vector NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (source, source_id)
    )
    """,
)


def create_schema(conn: Connection) -> None:
    for statement in SCHEMA_DDL:
        conn.execute(text(statement))


def index_exists(conn: Connection) -> bool:
    return fetch_one(conn, "SELECT to_regclass('rag.documents') IS NOT NULL AS present")["present"]


# --- sources -----------------------------------------------------------------


def list_item_sources(conn: Connection) -> list[dict[str, Any]]:
    """Catalogue fields of every item. Stock, prices and orders are deliberately excluded."""
    return fetch_all(
        conn,
        """
        SELECT i.id, i.item_code, i.name, i.status, i.unit, i.hsn_code, i.is_spare, i.notes,
               c.name AS category_name, sc.name AS sub_category_name, sc2.name AS sub_category_2_name
        FROM inv_items i
        LEFT JOIN inv_categories c ON c.id = i.category_id
        LEFT JOIN inv_sub_categories sc ON sc.id = i.sub_category_id
        LEFT JOIN inv_sub_categories_2 sc2 ON sc2.id = i.sub_category_2_id
        ORDER BY i.id
        """,
    )


def list_classification_sources(conn: Connection) -> list[dict[str, Any]]:
    """The item classification guide: one row per third-level sub-category."""
    return fetch_all(
        conn,
        """
        SELECT sc2.id, sc2.name, sc2.short_code, sc2.standard, sc2.description, sc2.naming_convention,
               sc2.convention_notes, sc2.examples, sc2.hsn_code,
               sc.name AS sub_category_name, c.name AS category_name
        FROM inv_sub_categories_2 sc2
        JOIN inv_sub_categories sc ON sc.id = sc2.sub_category_id
        JOIN inv_categories c ON c.id = sc.category_id
        ORDER BY sc2.id
        """,
    )


# --- index -------------------------------------------------------------------


def indexed_versions(conn: Connection) -> dict[tuple[str, str], tuple[str, str]]:
    """(source, source_id) -> (content_hash, embedding_model) for every indexed document."""
    rows = fetch_all(conn, "SELECT source, source_id, content_hash, embedding_model FROM rag.documents")
    return {(r["source"], r["source_id"]): (r["content_hash"], r["embedding_model"]) for r in rows}


def upsert_document(
    conn: Connection,
    *,
    source: str,
    source_id: str,
    title: str,
    content: str,
    metadata: dict[str, Any],
    content_hash: str,
    embedding_model: str,
    embedding: list[float],
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO rag.documents
                (source, source_id, title, content, metadata, content_hash, embedding_model, embedding, updated_at)
            VALUES (:source, :source_id, :title, :content, CAST(:metadata AS jsonb), :content_hash,
                    :embedding_model, CAST(:embedding AS rag.vector), now())
            ON CONFLICT (source, source_id) DO UPDATE SET
                title = EXCLUDED.title, content = EXCLUDED.content, metadata = EXCLUDED.metadata,
                content_hash = EXCLUDED.content_hash, embedding_model = EXCLUDED.embedding_model,
                embedding = EXCLUDED.embedding, updated_at = now()
            """
        ),
        {
            "source": source,
            "source_id": source_id,
            "title": title,
            "content": content,
            "metadata": json.dumps(metadata, sort_keys=True, default=str),
            "content_hash": content_hash,
            "embedding_model": embedding_model,
            "embedding": vector_literal(embedding),
        },
    )


def delete_missing(conn: Connection, source: str, keep_ids: list[str]) -> int:
    """Remove index rows of `source` whose source record no longer exists. Touches only rag.documents."""
    result = conn.execute(
        text("DELETE FROM rag.documents WHERE source = :source AND NOT (source_id = ANY(:keep))"),
        {"source": source, "keep": keep_ids},
    )
    return result.rowcount


def search(
    conn: Connection,
    embedding: list[float],
    *,
    embedding_model: str,
    min_similarity: float,
    limit: int,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Nearest documents by cosine similarity (1 - cosine distance), best first, above `min_similarity`."""
    return fetch_all(
        conn,
        """
        SELECT source, source_id, title, content, metadata, similarity
        FROM (
            SELECT source, source_id, title, content, metadata,
                   1 - (embedding OPERATOR(rag.<=>) CAST(:embedding AS rag.vector)) AS similarity
            FROM rag.documents
            WHERE embedding_model = :model AND (CAST(:source AS text) IS NULL OR source = :source)
        ) ranked
        WHERE similarity >= :min_similarity
        ORDER BY similarity DESC, source, source_id
        LIMIT :limit
        """,
        embedding=vector_literal(embedding),
        model=embedding_model,
        source=source,
        min_similarity=min_similarity,
        limit=limit,
    )


def vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
