"""Conversation persistence in the existing LangGraph checkpoint tables.

The tables (checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations)
already exist and hold legacy threads. This module never calls PostgresSaver.setup()
and never migrates: it only verifies that the recorded schema version is exactly the
one the installed package expects, and refuses to run otherwise.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from app.agent.errors import AgentConfigError
from app.config import get_settings

# Migrations are numbered from 0, so the latest version is len - 1 (9 for langgraph-checkpoint-postgres 3.1.2).
EXPECTED_SCHEMA_VERSION = len(PostgresSaver.MIGRATIONS) - 1


@contextmanager
def postgres_checkpointer(max_size: int = 5) -> Iterator[PostgresSaver]:
    """Yield a PostgresSaver on its own psycopg pool (the saver needs autocommit psycopg connections)."""
    # DATABASE_URL is stored with SQLAlchemy's driver prefix; psycopg wants the plain form.
    conninfo = make_url(get_settings().database_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    kwargs = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
    with ConnectionPool(conninfo, min_size=1, max_size=max_size, kwargs=kwargs, open=True) as pool:
        verify_schema_version(pool)
        yield PostgresSaver(pool)


def verify_schema_version(pool: ConnectionPool) -> None:
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT max(v) AS v FROM checkpoint_migrations").fetchone()
    except psycopg.Error:
        raise AgentConfigError("Conversation storage is unavailable") from None
    version = row["v"] if row else None
    if version != EXPECTED_SCHEMA_VERSION:
        raise AgentConfigError(
            f"Checkpoint schema version {version} does not match the expected {EXPECTED_SCHEMA_VERSION}; "
            "refusing to migrate existing conversation tables"
        )
