"""Indexing against the live database. Every test runs in a rolled-back transaction.

sync_index writes one row per changed document, so re-embedding the whole catalogue is
~550 sequential round trips to the remote database in one transaction (about a minute).
Only the model-change test needs that; the others sync under the model the index already
uses, so only the documents they change are written and their transactions stay short.
"""

import pytest
from sqlalchemy import text

from app.db.repositories import knowledge as repo
from app.rag.documents import ITEM, build_documents
from app.rag.embeddings import EmbeddingError
from app.rag.index import sync_index
from tests.fakes import FakeEmbedder


@pytest.fixture
def rag_conn(conn):
    if not repo.index_exists(conn):
        pytest.skip("semantic index not created (python -m app.rag.index --create-schema)")
    return conn


def index_rows(conn, model):
    return conn.execute(
        text("SELECT source, source_id, title, metadata, content_hash FROM rag.documents WHERE embedding_model = :m"),
        {"m": model},
    ).mappings().all()


def current_embedder(conn) -> FakeEmbedder:
    """Fake vectors under the index's own model: a sync then writes only what a test changed."""
    model = conn.execute(
        text("SELECT embedding_model FROM rag.documents GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
    ).scalar()
    return FakeEmbedder(model=model or "test-model")


def test_sync_embeds_new_then_nothing(rag_conn):
    embedder = FakeEmbedder()
    first = sync_index(rag_conn, embedder)
    documents = len(build_documents(rag_conn))
    # A different model than the stored one: everything is (re-)embedded once.
    assert (first.documents, first.embedded, first.unchanged) == (documents, documents, 0)
    assert len(index_rows(rag_conn, "test-model")) == documents

    second = sync_index(rag_conn, embedder)
    assert (second.embedded, second.unchanged, second.removed) == (0, documents, 0)
    assert len(embedder.calls) == 1  # unchanged content is never sent again


def test_changed_record_is_re_embedded_and_metadata_kept(rag_conn):
    embedder = current_embedder(rag_conn)
    sync_index(rag_conn, embedder)  # brings the index up to date first, normally writing nothing
    item = rag_conn.execute(text("SELECT id, item_code FROM inv_items ORDER BY id LIMIT 1")).mappings().one()
    rag_conn.execute(text("UPDATE inv_items SET name = name || ' (renamed)' WHERE id = :i"), {"i": item["id"]})

    report = sync_index(rag_conn, embedder)
    assert report.embedded == 1
    assert embedder.calls[-1][0].startswith("Item: ") and "(renamed)" in embedder.calls[-1][0]
    row = next(r for r in index_rows(rag_conn, embedder.model) if r["source_id"] == item["id"])
    assert row["source"] == ITEM and row["title"].endswith("(renamed)")
    assert row["metadata"]["item_code"] == item["item_code"] and row["metadata"]["item_id"] == item["id"]


def test_rows_for_deleted_sources_are_removed(rag_conn):
    embedder = current_embedder(rag_conn)
    sync_index(rag_conn, embedder)
    repo.upsert_document(
        rag_conn, source=ITEM, source_id="no-such-item", title="t", content="c", metadata={},
        content_hash="h", embedding_model=embedder.model, embedding=[1.0, 0.0, 0.0],
    )
    assert sync_index(rag_conn, embedder).removed == 1
    assert "no-such-item" not in {r["source_id"] for r in index_rows(rag_conn, embedder.model)}


def test_upsert_never_duplicates(rag_conn):
    for content in ("first", "second"):
        repo.upsert_document(
            rag_conn, source=ITEM, source_id="dup-test", title="t", content=content, metadata={"k": 1},
            content_hash=content, embedding_model="test-model", embedding=[0.0, 1.0, 0.0],
        )
    rows = rag_conn.execute(text("SELECT content FROM rag.documents WHERE source_id = 'dup-test'")).scalars().all()
    assert rows == ["second"]


def test_embedding_failure_leaves_index_unchanged(rag_conn):
    before = rag_conn.execute(text("SELECT count(*), md5(string_agg(content_hash, ',' ORDER BY source, source_id)) "
                                   "FROM rag.documents")).one()
    with pytest.raises(EmbeddingError):
        sync_index(rag_conn, FakeEmbedder(error=EmbeddingError("Semantic search is temporarily unavailable")))
    after = rag_conn.execute(text("SELECT count(*), md5(string_agg(content_hash, ',' ORDER BY source, source_id)) "
                                  "FROM rag.documents")).one()
    assert before == after
