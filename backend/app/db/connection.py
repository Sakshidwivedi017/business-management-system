"""PostgreSQL engine and connection helpers (SQLAlchemy Core, no ORM models).

The database schema is owned by an external system; this module never creates
or alters tables. The engine is created lazily on first use, so the app starts
even when the database is unreachable.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5},
    )


def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()


def get_connection() -> Iterator[Connection]:
    """FastAPI dependency: one pooled connection per request, rolled back unless committed."""
    with get_engine().connect() as conn:
        yield conn


@contextmanager
def atomic(conn: Connection) -> Iterator[None]:
    """Make a block all-or-nothing on `conn`.

    On a connection with no open transaction, this begins and commits one.
    Inside an existing transaction it uses a savepoint: a failure rolls back
    only this block, and committing stays with whoever opened the transaction.
    """
    if conn.in_transaction():
        with conn.begin_nested():
            yield
    else:
        with conn.begin():
            yield


def check_database() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        logger.exception("Database connectivity check failed")
        return False


def fetch_all(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def fetch_one(conn: Connection, sql: str, **params: Any) -> dict[str, Any] | None:
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None
