"""Layer 12: GET /api/dashboard over the real auth dependency and the real Layer 4 services.

Most tests replace the analytics repository queries with fixed rows and the engine with a
stand-in, so no database is touched. The live tests at the end call the endpoint with real
demo users' tokens against the live (read-only) database.
"""

import json
import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api import dashboard as dashboard_api
from app.auth import dependencies
from app.auth.dependencies import to_authenticated_user
from app.auth.permissions import PermissionDenied, Role
from app.auth.security import create_access_token
from app.db.connection import get_engine
from app.db.repositories import analytics as analytics_repo
from app.db.repositories.users import get_user_by_email
from app.services import dashboard as dashboard_ops
from tests.conftest import DEMO_EMAILS, assert_no_sensitive_data, make_user

URL = "/api/dashboard"

LOCATION_ID = str(uuid.uuid4())
VENDOR_ID = str(uuid.uuid4())

ROWS = {
    "item_counts": {"total_items": 501, "active_items": 500, "active_locations": 5},
    "low_stock_count": 149,
    "vendor_counts": {"total_vendors": 34, "active_vendors": 33},
    "purchase_orders_by_status": [
        {"status": "placed", "currency": "INR", "purchase_orders": 32, "total_amount": Decimal("4437362.15")},
        {"status": "placed", "currency": "USD", "purchase_orders": 1, "total_amount": Decimal("542.10")},
    ],
    "open_purchase_orders": [
        {"currency": "INR", "purchase_orders": 35, "total_amount": Decimal("4788746.73"),
         "lines_pending_delivery": Decimal("66")},
        {"currency": "USD", "purchase_orders": 1, "total_amount": Decimal("542.10"),
         "lines_pending_delivery": Decimal("1")},
    ],
    "stock_by_location": [
        {"location_id": LOCATION_ID, "location_name": "132-1", "stocked_items": 344, "items_in_stock": 266,
         "items_out_of_stock": 78},
    ],
    "stock_by_category": [{"category_id": None, "category_name": "Uncategorised", "active_items": 500,
                           "items_in_stock": 300}],
    "recent_transaction_counts": [{"transaction_type": "inbound", "transactions": 10}],
    "top_vendors_by_spend": [
        {"vendor_id": VENDOR_ID, "vendor_name": "Sterling Steels Pvt. Ltd.", "currency": "INR",
         "purchase_orders": 2, "total_amount": Decimal("3021209.47")},
    ],
}

INVENTORY = {"total_items": 501, "active_items": 500, "active_locations": 5,
             "low_stock": {"count": 149, "fallback_threshold": "0"}}
PROCUREMENT = {
    "total_vendors": 34,
    "active_vendors": 33,
    "purchase_orders_by_status": [
        {"status": "placed", "currency": "INR", "purchase_orders": 32, "total_amount": "4437362.15"},
        {"status": "placed", "currency": "USD", "purchase_orders": 1, "total_amount": "542.10"},
    ],
    "open_purchase_orders": [
        {"currency": "INR", "purchase_orders": 35, "total_amount": "4788746.73", "lines_pending_delivery": 66},
        {"currency": "USD", "purchase_orders": 1, "total_amount": "542.10", "lines_pending_delivery": 1},
    ],
}
ANALYTICS = {
    "stock_by_location": [{"location_name": "132-1", "stocked_items": 344, "items_in_stock": 266,
                           "items_out_of_stock": 78}],
    "recent_transactions": {"days": 30, "by_type": [{"transaction_type": "inbound", "transactions": 10}]},
    "top_vendors_by_spend": [{"vendor_name": "Sterling Steels Pvt. Ltd.", "currency": "INR", "purchase_orders": 2,
                              "total_amount": "3021209.47"}],
}
EXPECTED_SECTIONS = {
    Role.INVENTORY_MANAGER: {"inventory": INVENTORY, "procurement": None, "analytics": None},
    Role.PROCUREMENT_MANAGER: {"inventory": INVENTORY, "procurement": PROCUREMENT, "analytics": None},
    Role.OWNER: {"inventory": INVENTORY, "procurement": PROCUREMENT, "analytics": ANALYTICS},
}


class FakeEngine:
    """Stands in for an engine; tracks open connections. The connection itself is never used."""

    def __init__(self, error: Exception | None = None):
        self.open = 0
        self.connects = 0
        self.error = error
        self.on_connect = None

    @contextmanager
    def connect(self):
        if self.on_connect:
            self.on_connect()
        if self.error:
            raise self.error
        self.connects += 1
        self.open += 1
        try:
            yield None
        finally:
            self.open -= 1


@pytest.fixture
def queries(monkeypatch):
    """Fixed results for every analytics repository query; records which ran."""
    called: list[str] = []

    def fake(name):
        def query(_conn, *args, **kwargs):
            called.append(name)
            return ROWS[name]
        return query

    for name in ROWS:
        monkeypatch.setattr(analytics_repo, name, fake(name))
    return called


@pytest.fixture
def engines(monkeypatch):
    auth, data = FakeEngine(), FakeEngine()
    monkeypatch.setattr(dependencies, "get_engine", lambda: auth)
    monkeypatch.setattr(dashboard_api, "get_engine", lambda: data)
    return auth, data


@pytest.fixture
def login(fake_users, engines):
    """login(role) -> (users row, headers) for a user known only to the in-memory store."""

    def make(role: Role, **kwargs):
        row = fake_users(make_user(role.value, **kwargs))
        token = create_access_token(row["id"], row["token_version"])[0]
        return row, {"Authorization": f"Bearer {token}"}

    return make


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic abc"}])
def test_unauthenticated_requests_rejected(client, fake_users, engines, queries, headers):
    response = client.get(URL, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}
    assert engines[1].connects == 0 and queries == []


@pytest.mark.parametrize("change", [{"is_active": False}, {"token_version": 1}])
def test_deactivated_or_revoked_users_rejected(client, login, queries, change):
    row, headers = login(Role.OWNER)
    row.update(change)
    assert client.get(URL, headers=headers).status_code == 401
    assert queries == []


# --- role-aware sections -----------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_each_role_receives_its_sections(client, login, queries, role):
    row, headers = login(role)
    response = client.get(URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {"user": {"full_name": row["full_name"], "role": role.value}, **EXPECTED_SECTIONS[role]}


def test_inventory_manager_gets_no_procurement_or_analytics_data(client, login, queries):
    _, headers = login(Role.INVENTORY_MANAGER)
    response = client.get(URL, headers=headers)

    body = response.json()
    assert body["procurement"] is None and body["analytics"] is None
    # Not even queried, so nothing procurement-related can leak.
    assert set(queries) == {"item_counts", "low_stock_count"}
    for marker in ("vendor", "purchase_order", "currency", "Sterling", "INR"):
        assert marker not in response.text


def test_procurement_manager_gets_no_owner_analytics(client, login, queries):
    _, headers = login(Role.PROCUREMENT_MANAGER)
    response = client.get(URL, headers=headers)

    body = response.json()
    assert body["inventory"] == INVENTORY and body["procurement"] == PROCUREMENT and body["analytics"] is None
    assert set(queries) == {"item_counts", "low_stock_count", "vendor_counts", "purchase_orders_by_status",
                            "open_purchase_orders"}
    for marker in ("top_vendors", "Sterling", "stock_by_location", "132-1", "recent_transactions"):
        assert marker not in response.text


def test_owner_gets_the_broader_overview_from_the_owner_summaries(client, login, queries):
    _, headers = login(Role.OWNER)
    body = client.get(URL, headers=headers).json()

    assert body["analytics"] == ANALYTICS
    # Built from inventory_summary and procurement_summary: each query runs once.
    assert sorted(queries) == sorted(ROWS)


def test_client_supplied_role_or_user_cannot_change_the_response(client, login, queries):
    owner, _ = login(Role.OWNER)
    _, headers = login(Role.INVENTORY_MANAGER)
    response = client.request(
        "GET",
        URL,
        params={"role": "owner", "user_id": str(owner["id"])},
        headers={**headers, "X-Role": "owner", "X-User-Id": str(owner["id"])},
        json={"role": "owner", "user_id": str(owner["id"])},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["role"] == "inventory_manager"
    assert body["procurement"] is None and body["analytics"] is None


def test_no_sensitive_or_internal_fields(client, login, queries):
    row, headers = login(Role.OWNER)
    response = client.get(URL, headers=headers)

    assert_no_sensitive_data(response.text)
    keys = set(walk_keys(response.json()))
    assert not keys & {"id", "location_id", "vendor_id", "category_id", "email", "password", "token",
                       "access_token", "token_version", "is_active"}
    for value in (str(row["id"]), row["email"], LOCATION_ID, VENDOR_ID, headers["Authorization"][7:]):
        assert value not in response.text


def test_amounts_keep_exact_decimals_and_their_currency(client, login, queries):
    _, headers = login(Role.OWNER)
    body = client.get(URL, headers=headers).json()
    for row in body["procurement"]["purchase_orders_by_status"] + body["procurement"]["open_purchase_orders"]:
        assert isinstance(row["total_amount"], str) and row["currency"] in {"INR", "USD"}


# --- connections and failures --------------------------------------------------------------


def test_auth_connection_released_and_one_short_connection_used(client, login, engines, queries):
    auth, data = engines
    _, headers = login(Role.OWNER)
    seen = []
    data.on_connect = lambda: seen.append(auth.open)

    assert client.get(URL, headers=headers).status_code == 200
    assert seen == [0]  # the auth lookup's connection was back in the pool first
    assert data.connects == 1 and data.open == 0


@pytest.mark.parametrize(
    "error",
    [OperationalError("SELECT secret_column FROM users", {}, Exception("password=hunter2")),
     RuntimeError("Traceback: secret internals")],
)
def test_query_failures_map_to_safe_errors(client, login, queries, monkeypatch, caplog, error):
    def broken(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(analytics_repo, "item_counts", broken)
    _, headers = login(Role.INVENTORY_MANAGER)
    response = client.get(URL, headers=headers)

    # Database errors are sanitized by Layer 4 (503); anything unexpected is a generic 500.
    assert response.status_code == (503 if isinstance(error, OperationalError) else 500)
    assert response.json() == {"detail": "The dashboard is temporarily unavailable; please try again"}
    for leak in ("secret", "hunter2", "Traceback", "SELECT"):
        assert leak not in response.text


def test_unreachable_database_is_unavailable(client, fake_users, monkeypatch, engines):
    monkeypatch.setattr(dashboard_api, "get_engine", lambda: FakeEngine(OperationalError("connect", {}, Exception("host=db.internal"))))
    row = fake_users(make_user(Role.OWNER.value))
    token = create_access_token(row["id"], row["token_version"])[0]
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert "db.internal" not in response.text


def test_permission_denied_maps_to_403(client, login, monkeypatch):
    def denied(_conn, user):
        raise PermissionDenied(user.role, dashboard_ops.Permission.ANALYTICS_READ)

    monkeypatch.setattr(dashboard_api, "dashboard_overview", denied)
    _, headers = login(Role.OWNER)
    response = client.get(URL, headers=headers)
    assert response.status_code == 403 and response.json() == {"detail": "Forbidden"}


# --- the services check permissions themselves -----------------------------------------------


def test_overview_services_enforce_their_permissions():
    inventory_manager = to_authenticated_user(make_user(Role.INVENTORY_MANAGER.value))
    # The permission check runs before any database access, so no connection is needed.
    with pytest.raises(PermissionDenied):
        dashboard_ops.procurement_overview(None, inventory_manager)


# --- live database (read-only) ---------------------------------------------------------------


def live_headers(conn, role: str) -> dict[str, str]:
    row = get_user_by_email(conn, DEMO_EMAILS[role])
    return {"Authorization": f"Bearer {create_access_token(row['id'], row['token_version'])[0]}"}


def scalar(conn, sql):
    return conn.execute(text(sql)).scalar_one()


@pytest.mark.parametrize("role", ["inventory_manager", "procurement_manager", "owner"])
def test_live_dashboard_for_each_demo_role(client, live_db, role):
    with get_engine().connect() as conn:
        headers = live_headers(conn, role)
        expected = {
            "total_items": scalar(conn, "SELECT count(*) FROM inv_items"),
            "active_items": scalar(conn, "SELECT count(*) FROM inv_items WHERE status = 'active'"),
            "active_locations": scalar(conn, "SELECT count(*) FROM inv_locations WHERE is_active"),
            "vendors": scalar(conn, "SELECT count(*) FROM proc_vendors"),
            "purchase_orders": scalar(conn, "SELECT count(*) FROM proc_purchase_orders"),
        }

    response = client.get(URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["role"] == role
    inventory = body["inventory"]
    assert {k: inventory[k] for k in ("total_items", "active_items", "active_locations")} == {
        k: expected[k] for k in ("total_items", "active_items", "active_locations")
    }
    assert inventory["low_stock"]["count"] >= 0
    if role == "inventory_manager":
        assert body["procurement"] is None
    else:
        assert body["procurement"]["total_vendors"] == expected["vendors"]
        assert sum(r["purchase_orders"] for r in body["procurement"]["purchase_orders_by_status"]) == (
            expected["purchase_orders"]
        )
    assert (body["analytics"] is not None) == (role == "owner")
    assert_no_sensitive_data(response.text)
    assert "_id" not in json.dumps(body)
