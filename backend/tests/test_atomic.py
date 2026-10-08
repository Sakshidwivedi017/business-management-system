"""atomic(): commit/rollback semantics, exercised on a session-local TEMP table only."""

import pytest
from sqlalchemy import text

from app.db.connection import atomic, get_engine


@pytest.fixture
def temp_conn(live_db):
    with get_engine().connect() as conn:
        conn.execute(text("CREATE TEMP TABLE atomic_probe (n int) ON COMMIT PRESERVE ROWS"))
        conn.commit()
        yield conn
        conn.rollback()
        # Pooled connections keep their session, so drop the temp table explicitly.
        conn.execute(text("DROP TABLE atomic_probe"))
        conn.commit()


def rows(conn):
    return conn.execute(text("SELECT n FROM atomic_probe ORDER BY n")).scalars().all()


def test_fresh_connection_commits(temp_conn):
    assert not temp_conn.in_transaction()
    with atomic(temp_conn):
        temp_conn.execute(text("INSERT INTO atomic_probe VALUES (1)"))
    assert not temp_conn.in_transaction()  # committed, not left open
    temp_conn.rollback()
    assert rows(temp_conn) == [1]


def test_fresh_connection_rolls_back_on_error(temp_conn):
    with pytest.raises(RuntimeError):
        with atomic(temp_conn):
            temp_conn.execute(text("INSERT INTO atomic_probe VALUES (2)"))
            raise RuntimeError
    assert rows(temp_conn) == []


def test_inside_transaction_uses_savepoint(temp_conn):
    outer = temp_conn.begin()
    temp_conn.execute(text("INSERT INTO atomic_probe VALUES (3)"))
    with pytest.raises(RuntimeError):
        with atomic(temp_conn):
            temp_conn.execute(text("INSERT INTO atomic_probe VALUES (4)"))
            raise RuntimeError
    assert rows(temp_conn) == [3]  # only the failed block was undone
    with atomic(temp_conn):
        temp_conn.execute(text("INSERT INTO atomic_probe VALUES (5)"))
    assert temp_conn.in_transaction()  # caller still owns the commit
    outer.rollback()
    assert rows(temp_conn) == []
