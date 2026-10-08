import uuid
from functools import cache
from typing import Any

import bcrypt
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import dependencies, security
from app.auth.dependencies import to_authenticated_user
from app.config import get_settings
from app.db.connection import check_database, get_connection, get_engine
from app.db.repositories.users import get_user_by_email
from app.main import app

# Tests that use one of these fixtures (directly or through another fixture) need the live database.
DB_FIXTURES = {"live_db", "conn", "demo_users"}


@cache
def db_available() -> bool:
    """Checked on first need, so a run that selects no database test never connects."""
    return check_database()


def pytest_collection_modifyitems(items):
    for item in items:
        if DB_FIXTURES & set(item.fixturenames):
            item.add_marker(pytest.mark.db)

DEMO_EMAILS = {
    "owner": "owner@gmail.com",
    "procurement_manager": "procurement@gmail.com",
    "inventory_manager": "inventory@gmail.com",
}

# Tables Layer 4 can write to, plus the auth tables: content-hashed before/after the run.
HASHED_TABLES = (
    "users", "refresh_tokens", "agent_audit_log", "inv_current_stock", "inv_transactions",
    "inv_items", "inv_locations", "proc_vendors", "proc_purchase_orders", "proc_po_lines", "proc_po_receipts",
)


def make_user(
    role: str,
    password: str = "correct horse battery staple",
    *,
    is_active: bool = True,
    token_version: int = 0,
) -> dict[str, Any]:
    """In-memory users row, shaped like app.db.repositories.users returns it."""
    return {
        "id": uuid.uuid4(),
        "email": f"{role}.{uuid.uuid4().hex[:8]}@test.local",
        # Low cost factor keeps tests fast; production hashes use 12.
        "password_hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode(),
        "full_name": f"Test {role}",
        "role": role,
        "is_active": is_active,
        "token_version": token_version,
    }


def assert_no_sensitive_data(body: str) -> None:
    # Checked as a bare boolean so a failure never prints the secret.
    secret_leaked = get_settings().jwt_secret_key in body
    assert not secret_leaked, "response leaked JWT_SECRET_KEY"
    for marker in ("password_hash", "$2b$", "token_version"):
        assert marker not in body, f"response leaked {marker!r}"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_users(monkeypatch):
    """Replace user lookups with an in-memory store; no database access."""
    store: dict[str, dict[str, Any]] = {}

    def by_email(_conn, email):
        return next((u for u in store.values() if u["email"] == email.strip().lower()), None)

    monkeypatch.setattr(security, "get_user_by_email", by_email)
    monkeypatch.setattr(dependencies, "get_user_by_id", lambda _conn, user_id: store.get(str(user_id)))
    app.dependency_overrides[get_connection] = lambda: None

    def add(user: dict[str, Any]) -> dict[str, Any]:
        store[str(user["id"])] = user
        return user

    yield add
    app.dependency_overrides.pop(get_connection, None)


@pytest.fixture
def live_db():
    if not db_available():
        pytest.skip("live database unreachable")


@pytest.fixture
def conn(live_db):
    """Live connection inside a transaction that is always rolled back."""
    with get_engine().connect() as connection:
        outer = connection.begin()
        try:
            yield connection
        finally:
            outer.rollback()


@pytest.fixture(scope="session")
def demo_users():
    """Real demo users as AuthenticatedUser, keyed by role (audit rows reference users.id)."""
    if not db_available():
        pytest.skip("live database unreachable")
    with get_engine().connect() as connection:
        rows = {role: get_user_by_email(connection, email) for role, email in DEMO_EMAILS.items()}
    missing = sorted(DEMO_EMAILS[role] for role, row in rows.items() if row is None)
    if missing:
        pytest.fail(f"demo user(s) not found in the database: {', '.join(missing)}", pytrace=False)
    return {role: to_authenticated_user(row) for role, row in rows.items()}


def _database_fingerprint() -> dict[str, Any]:
    """Schema hash, row count of every public table, and content hashes of HASHED_TABLES."""
    with get_engine().connect() as conn:
        schema = conn.execute(
            text(
                """
                SELECT md5(string_agg(d, '|' ORDER BY d)) FROM (
                    SELECT concat_ws(':', table_name, column_name, data_type, is_nullable, column_default) AS d
                    FROM information_schema.columns WHERE table_schema = 'public'
                    UNION ALL
                    SELECT concat_ws(':', conrelid::regclass::text, conname, pg_get_constraintdef(oid))
                    FROM pg_constraint WHERE connamespace = 'public'::regnamespace
                    UNION ALL
                    SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'
                ) s
                """
            )
        ).scalar_one()
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars().all()
        fingerprint: dict[str, Any] = {
            t: conn.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar_one() for t in tables
        }
        fingerprint["schema:md5"] = schema
        for t in HASHED_TABLES:
            fingerprint[f"{t}:md5"] = conn.execute(
                text(f"SELECT md5(coalesce(string_agg(t::text, ',' ORDER BY t.id), '')) FROM public.{t} t")
            ).scalar_one()
        # The semantic index (Layer 6), which the RAG tests rewrite inside rolled-back transactions.
        if conn.execute(text("SELECT to_regclass('rag.documents')")).scalar() is not None:
            fingerprint["rag.documents:md5"] = conn.execute(
                text(
                    "SELECT count(*) || ':' || md5(coalesce(string_agg(concat_ws(':', source, source_id, "
                    "content_hash, embedding_model), ',' ORDER BY source, source_id), '')) FROM rag.documents"
                )
            ).scalar_one()
    return fingerprint


def _block_database(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every PostgreSQL connection attempt fail loudly, and record it.

    Both paths to the server go through psycopg: SQLAlchemy's engine and the checkpointer's pool.
    """
    attempts: list[str] = []

    def refuse(*args, **kwargs):
        attempts.append("connect")
        raise AssertionError("a test without the db marker tried to connect to the database")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refuse))
    return attempts


@pytest.fixture(scope="session", autouse=True)
def database_unchanged(request):
    """Layer 3 must be read-only: the database must look identical after the run.

    When the selection holds no database test (pytest -m "not db"), the database is not
    fingerprinted but blocked instead: nothing can connect, so nothing can change it, and a
    test that needs the database without being marked for it fails the run.
    """
    if not any(item.get_closest_marker("db") for item in request.session.items):
        with pytest.MonkeyPatch.context() as monkeypatch:
            attempts = _block_database(monkeypatch)
            yield
        assert not attempts, f"{len(attempts)} database connection attempt(s) in an offline run"
        return
    if not db_available():
        yield
        return
    before = _database_fingerprint()
    yield
    after = _database_fingerprint()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, f"database changed during tests: {changed}"
