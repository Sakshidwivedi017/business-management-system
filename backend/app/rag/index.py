"""Build or update the semantic index. Run explicitly; never at application startup.

    python -m app.rag.index --create-schema   # first time: rag schema, pgvector extension, rag.documents
    python -m app.rag.index                   # later: embed only new or changed records

Only documents whose text or embedding model changed are re-embedded, and
index rows whose source record no longer exists are removed. Business tables
are only read.
"""

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import Connection

from app.db.connection import atomic, get_engine
from app.db.repositories import knowledge as repo
from app.rag.documents import SOURCES, build_documents
from app.rag.embeddings import Embedder, EmbeddingError, get_embedder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexReport:
    documents: int
    embedded: int
    unchanged: int
    removed: int


def sync_index(conn: Connection, embedder: Embedder) -> IndexReport:
    documents = build_documents(conn)
    indexed = repo.indexed_versions(conn)
    changed = [d for d in documents if indexed.get((d.source, d.source_id)) != (d.content_hash, embedder.model)]
    # Embed before writing, so a failed API call leaves the index as it was.
    vectors = embedder.embed([d.content for d in changed]) if changed else []
    removed = 0
    with atomic(conn):
        for doc, vector in zip(changed, vectors, strict=True):
            repo.upsert_document(
                conn,
                source=doc.source,
                source_id=doc.source_id,
                title=doc.title,
                content=doc.content,
                metadata=doc.metadata,
                content_hash=doc.content_hash,
                embedding_model=embedder.model,
                embedding=vector,
            )
        for source in SOURCES:
            removed += repo.delete_missing(conn, source, [d.source_id for d in documents if d.source == source])
    report = IndexReport(len(documents), len(changed), len(documents) - len(changed), removed)
    logger.info("Semantic index synced: %s", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rag.index", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="create the rag schema, pgvector extension and rag.documents table if missing (additive, idempotent)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    with get_engine().begin() as conn:
        if args.create_schema:
            repo.create_schema(conn)
        elif not repo.index_exists(conn):
            print("The semantic index does not exist yet; run with --create-schema first.", file=sys.stderr)
            return 1
    try:
        with get_engine().begin() as conn:
            report = sync_index(conn, get_embedder())
    except EmbeddingError as exc:
        print(f"Indexing failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Indexed {report.documents} documents: {report.embedded} embedded, "
        f"{report.unchanged} unchanged, {report.removed} removed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
