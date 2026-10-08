"""Inventory operations against the live database. Every test runs in a rolled-back transaction."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.auth.permissions import AuthenticatedUser, PermissionDenied, Role
from app.db.repositories import audit as audit_repo
from app.services import (
    ConflictError,
    InsufficientStockError,
    NotFoundError,
    OperationFailedError,
    ValidationError,
)
from app.services import inventory as ops
from app.services.inventory import LOW_STOCK_FALLBACK_THRESHOLD


def one(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().first()


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


@pytest.fixture
def inv_user(demo_users):
    return demo_users["inventory_manager"]


@pytest.fixture
def stocked(conn):
    """An active item with positive stock at an active location."""
    row = one(
        conn,
        """
        SELECT s.item_id, i.item_code, s.location_id, l.name AS location_name, s.quantity
        FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id
        JOIN inv_locations l ON l.id = s.location_id
        WHERE i.status = 'active' AND l.is_active AND s.quantity >= 10
        ORDER BY s.item_id LIMIT 1
        """,
    )
    assert row is not None
    return row


def stock_qty(conn, item_id, location_id):
    return conn.execute(
        text("SELECT quantity FROM inv_current_stock WHERE item_id = :i AND location_id = :l"),
        {"i": item_id, "l": location_id},
    ).scalar()


def counts(conn):
    return {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in ("inv_transactions", "agent_audit_log", "inv_current_stock")}


# --- reads -------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_every_role_can_read_inventory(conn, demo_users, role, stocked):
    user = demo_users[role.value]
    details = ops.get_item_details(conn, user, stocked["item_id"])
    assert details["item"]["id"] == stocked["item_id"]


def test_search_items(conn, inv_user, stocked):
    results = ops.search_items(conn, inv_user, stocked["item_code"])
    assert stocked["item_id"] in {r["id"] for r in results}


def test_item_lookup_by_id_and_code(conn, inv_user, stocked):
    by_id = ops.get_item_details(conn, inv_user, stocked["item_id"])
    by_code = ops.get_item_details(conn, inv_user, stocked["item_code"])
    assert by_id == by_code
    expected_total = scalar(
        conn, "SELECT sum(quantity) FROM inv_current_stock WHERE item_id = :i", i=stocked["item_id"]
    )
    assert by_id["total_quantity"] == Decimal(str(expected_total))
    assert {s["location_id"] for s in by_id["stock"]} >= {stocked["location_id"]}


def test_item_not_found(conn, inv_user):
    with pytest.raises(NotFoundError):
        ops.get_item_details(conn, inv_user, "NO-SUCH-ITEM")


def test_stock_by_location_filters(conn, inv_user, stocked):
    result = ops.get_stock_by_location(conn, inv_user, stocked["location_name"], limit=200)
    assert result["location"]["id"] == stocked["location_id"]
    assert result["stock"] and all(r["location_id"] == stocked["location_id"] for r in result["stock"])


def test_transaction_history_filters(conn, inv_user):
    pair = one(conn, "SELECT item_id, location_id FROM inv_transactions WHERE location_id IS NOT NULL LIMIT 1")
    rows = ops.get_transaction_history(conn, inv_user, item=pair["item_id"], location=pair["location_id"])
    assert rows
    assert all(r["item_id"] == pair["item_id"] and r["location_id"] == pair["location_id"] for r in rows)
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)


PO_ID = str(uuid.uuid4())
PO_RECEIPT_TXN = {
    "id": str(uuid.uuid4()), "item_id": "item-1", "item_code": "BO-MC-0172", "item_name": "Bolt",
    "location_id": "loc-1", "location_name": "103-1", "transaction_type": "inbound", "quantity": 6.0,
    "unit_cost": 18.06, "currency": "INR", "reference_type": "purchase_order", "reference_id": PO_ID,
    "transfer_location_id": None, "issued_to_id": None, "issued_to_name": None, "notes": "invoice no 2307",
    "created_by": "user-1", "created_at": "2026-10-01 09:30:00",
}
ISSUE_TXN = {
    **PO_RECEIPT_TXN, "id": str(uuid.uuid4()), "transaction_type": "outbound", "quantity": -2.0, "unit_cost": None,
    "reference_type": "warehouse_issue", "reference_id": None, "notes": "so no 13528",
}


def agent_user(role: Role) -> AuthenticatedUser:
    return AuthenticatedUser(id=uuid.uuid4(), email=f"{role.value}@test.local", full_name="Test", role=role)


@pytest.fixture
def stub_transactions(monkeypatch):
    rows = [PO_RECEIPT_TXN, ISSUE_TXN]
    monkeypatch.setattr(ops.repo, "get_transactions", lambda conn, **kwargs: [dict(row) for row in rows])
    return rows


def test_transaction_history_hides_procurement_fields_from_inventory_manager(stub_transactions):
    receipt, issue = ops.get_transaction_history(None, agent_user(Role.INVENTORY_MANAGER))
    for field in ops.PROCUREMENT_TRANSACTION_FIELDS:
        assert receipt[field] is None
    # Everything else about the movement is still there.
    assert {k: v for k, v in receipt.items() if k not in ops.PROCUREMENT_TRANSACTION_FIELDS} == {
        k: v for k, v in PO_RECEIPT_TXN.items() if k not in ops.PROCUREMENT_TRANSACTION_FIELDS
    }
    # Transactions that are not purchase-order receipts are unchanged.
    assert issue == ISSUE_TXN
    text_ = str([receipt, issue])
    for leak in (PO_ID, "18.06", "invoice no 2307", "purchase_order"):
        assert leak not in text_


@pytest.mark.parametrize("role", [Role.PROCUREMENT_MANAGER, Role.OWNER])
def test_transaction_history_is_complete_with_procurement_read(stub_transactions, role):
    assert ops.get_transaction_history(None, agent_user(role)) == [PO_RECEIPT_TXN, ISSUE_TXN]


@pytest.mark.parametrize("role", list(Role))
def test_transaction_history_purchase_receipts_by_role(conn, role):
    # Reads write nothing, so an in-memory user of each role is enough.
    receipt = one(
        conn,
        "SELECT item_id, location_id FROM inv_transactions WHERE reference_type = 'purchase_order'"
        " AND unit_cost IS NOT NULL AND notes IS NOT NULL LIMIT 1",
    )
    assert receipt is not None
    stored = ops.repo.get_transactions(conn, item_id=receipt["item_id"], location_id=receipt["location_id"], limit=50)
    rows = ops.get_transaction_history(
        conn, agent_user(role), item=receipt["item_id"], location=receipt["location_id"], limit=50
    )
    assert [r["id"] for r in rows] == [r["id"] for r in stored]
    if role is Role.INVENTORY_MANAGER:
        for row, original in zip(rows, stored, strict=True):
            if original["reference_type"] == "purchase_order":
                assert all(row[field] is None for field in ops.PROCUREMENT_TRANSACTION_FIELDS)
                assert row["quantity"] == original["quantity"] and row["created_at"] == original["created_at"]
            else:
                assert row == original
    else:
        assert rows == stored


def test_low_stock_uses_fallback_when_min_level_unset(conn, inv_user):
    result = ops.get_low_stock_items(conn, inv_user, limit=200)
    assert result["fallback_threshold"] == LOW_STOCK_FALLBACK_THRESHOLD
    expected = scalar(
        conn,
        """
        SELECT count(*) FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id
        JOIN inv_locations l ON l.id = s.location_id
        WHERE i.status = 'active' AND l.is_active AND s.min_stock_level IS NULL AND s.quantity <= 0
        """,
    )
    assert len(result["items"]) == expected > 0
    assert all(r["threshold_source"] == "fallback" and r["quantity"] <= 0 for r in result["items"])


def test_low_stock_custom_threshold_and_min_level(conn, inv_user, stocked):
    threshold = Decimal("5")
    custom = ops.get_low_stock_items(conn, inv_user, threshold=threshold, location=stocked["location_id"], limit=200)
    expected = scalar(
        conn,
        """
        SELECT count(*) FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id
        WHERE s.location_id = :l AND i.status = 'active' AND s.quantity <= 5
        """,
        l=stocked["location_id"],
    )
    assert custom["fallback_threshold"] == threshold
    assert len(custom["items"]) == min(expected, 200)
    assert all(r["quantity"] <= 5 and r["location_id"] == stocked["location_id"] for r in custom["items"])

    # A configured min_stock_level overrides the fallback (set only inside this rolled-back transaction).
    conn.execute(
        text("UPDATE inv_current_stock SET min_stock_level = :m WHERE item_id = :i AND location_id = :l"),
        {"m": stocked["quantity"] + 1, "i": stocked["item_id"], "l": stocked["location_id"]},
    )
    result = ops.get_low_stock_items(conn, inv_user, location=stocked["location_id"], limit=200)
    row = next(r for r in result["items"] if r["item_id"] == stocked["item_id"])
    assert row["threshold_source"] == "min_stock_level"


def test_low_stock_query_filter(conn, inv_user):
    sample = ops.get_low_stock_items(conn, inv_user, limit=1)["items"][0]
    word = sample["item_name"].split()[0]
    result = ops.get_low_stock_items(conn, inv_user, query=word.lower(), limit=200)
    expected = scalar(
        conn,
        """
        SELECT count(*) FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id
        JOIN inv_locations l ON l.id = s.location_id
        WHERE i.status = 'active' AND l.is_active AND s.quantity <= COALESCE(s.min_stock_level, 0)
          AND (i.name ILIKE :p OR i.item_code ILIKE :p)
        """,
        p=f"%{word}%",
    )
    assert len(result["items"]) == min(expected, 200) > 0
    assert all(word.lower() in f"{r['item_name']} {r['item_code']}".lower() for r in result["items"])
    assert ops.get_low_stock_items(conn, inv_user, query="NO-SUCH-ITEM-XYZ")["items"] == []
    # Blank query means no filter.
    assert len(ops.get_low_stock_items(conn, inv_user, query="  ", limit=200)["items"]) == len(
        ops.get_low_stock_items(conn, inv_user, limit=200)["items"]
    )


def test_list_locations(conn, inv_user):
    active = ops.list_locations(conn, inv_user)
    every = ops.list_locations(conn, inv_user, active_only=False)
    assert active and all(loc["is_active"] for loc in active)
    assert len(every) == scalar(conn, "SELECT count(*) FROM inv_locations") >= len(active)
    assert set(every[0]) == {"id", "name", "address", "is_active"}


# --- record_stock_movement ---------------------------------------------------


@pytest.mark.parametrize(("direction", "sign"), [("inbound", 1), ("outbound", -1)])
def test_stock_movement(conn, inv_user, stocked, direction, sign):
    before = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    result = ops.record_stock_movement(
        conn, inv_user, item=stocked["item_code"], location=stocked["location_name"],
        direction=direction, quantity="2.5", notes="layer4 test",
    )
    after = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    assert Decimal(str(after)) == Decimal(str(before)) + sign * Decimal("2.5")
    assert result["quantity_after"] == Decimal(str(after))
    assert result["quantity_before"] == Decimal(str(before))

    txn = one(conn, "SELECT * FROM inv_transactions WHERE id = :id", id=result["transaction"]["id"])
    assert txn["item_id"] == stocked["item_id"] and txn["location_id"] == stocked["location_id"]
    assert txn["transaction_type"] == direction
    assert txn["quantity"] == sign * 2.5
    assert txn["created_by"] == str(inv_user.id)
    assert txn["notes"] == "layer4 test"
    assert txn["currency"] == "INR"

    audit = one(conn, "SELECT * FROM agent_audit_log WHERE operation_id = :o", o=result["audit_operation_id"])
    assert audit["user_id"] == inv_user.id
    assert audit["role"] == "inventory_manager"
    assert audit["operation"] == "record_stock_movement"
    assert audit["entity_type"] == "inventory_transaction" and audit["entity_id"] == txn["id"]
    assert audit["status"] == "success" and audit["thread_id"] is None and audit["error"] is None
    assert Decimal(audit["after_state"]["quantity"]) == Decimal(str(after))
    assert audit["completed_at"] is not None


def test_inbound_creates_missing_stock_row(conn, inv_user):
    pair = one(
        conn,
        """
        SELECT i.id AS item_id, l.id AS location_id FROM inv_items i CROSS JOIN inv_locations l
        WHERE i.status = 'active' AND l.is_active AND NOT EXISTS (
            SELECT 1 FROM inv_current_stock s WHERE s.item_id = i.id AND s.location_id = l.id)
        LIMIT 1
        """,
    )
    result = ops.record_stock_movement(
        conn, inv_user, item=pair["item_id"], location=pair["location_id"], direction="inbound", quantity=4
    )
    assert result["quantity_before"] == 0 and result["quantity_after"] == 4
    assert stock_qty(conn, pair["item_id"], pair["location_id"]) == 4


def test_outbound_to_exactly_zero_allowed(conn, inv_user, stocked):
    qty = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    result = ops.record_stock_movement(
        conn, inv_user, item=stocked["item_id"], location=stocked["location_id"],
        direction="outbound", quantity=Decimal(str(qty)),
    )
    assert result["quantity_after"] == 0


def test_negative_stock_rejected_without_changes(conn, inv_user, stocked):
    before_qty = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    before = counts(conn)
    with pytest.raises(InsufficientStockError):
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location=stocked["location_id"],
            direction="outbound", quantity=Decimal(str(before_qty)) + Decimal("0.001"),
        )
    assert stock_qty(conn, stocked["item_id"], stocked["location_id"]) == before_qty
    assert counts(conn) == before


def test_outbound_with_no_stock_row_rejected(conn, inv_user):
    pair = one(
        conn,
        """
        SELECT i.id AS item_id, l.id AS location_id FROM inv_items i CROSS JOIN inv_locations l
        WHERE i.status = 'active' AND l.is_active AND NOT EXISTS (
            SELECT 1 FROM inv_current_stock s WHERE s.item_id = i.id AND s.location_id = l.id)
        LIMIT 1
        """,
    )
    before = counts(conn)
    with pytest.raises(InsufficientStockError):
        ops.record_stock_movement(
            conn, inv_user, item=pair["item_id"], location=pair["location_id"], direction="outbound", quantity=1
        )
    assert counts(conn) == before


@pytest.mark.parametrize(
    "quantity", [0, -1, "-0.5", "abc", None, True, float("nan"), float("inf"), "1.0001", 10**12, [1]]
)
def test_invalid_quantity(conn, inv_user, stocked, quantity):
    with pytest.raises(ValidationError):
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location=stocked["location_id"],
            direction="inbound", quantity=quantity,
        )


@pytest.mark.parametrize("direction", ["in", "INBOUND", "transfer", "", None, 1])
def test_invalid_direction(conn, inv_user, stocked, direction):
    with pytest.raises(ValidationError):
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location=stocked["location_id"],
            direction=direction, quantity=1,
        )


def test_invalid_item(conn, inv_user, stocked):
    with pytest.raises(NotFoundError):
        ops.record_stock_movement(
            conn, inv_user, item="NO-SUCH-ITEM", location=stocked["location_id"], direction="inbound", quantity=1
        )
    inactive = scalar(conn, "SELECT id FROM inv_items WHERE status <> 'active' LIMIT 1")
    with pytest.raises(ValidationError):
        ops.record_stock_movement(
            conn, inv_user, item=inactive, location=stocked["location_id"], direction="inbound", quantity=1
        )


def test_invalid_location(conn, inv_user, stocked):
    with pytest.raises(NotFoundError):
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location="NO-SUCH-LOC", direction="inbound", quantity=1
        )
    inactive = scalar(conn, "SELECT id FROM inv_locations WHERE NOT is_active LIMIT 1")
    with pytest.raises(ValidationError):
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location=inactive, direction="inbound", quantity=1
        )


@pytest.mark.parametrize("role", ["procurement_manager", "owner"])
def test_unauthorized_stock_movement(conn, demo_users, stocked, role):
    before = counts(conn)
    with pytest.raises(PermissionDenied):
        ops.record_stock_movement(
            conn, demo_users[role], item=stocked["item_id"], location=stocked["location_id"],
            direction="inbound", quantity=1,
        )
    assert counts(conn) == before


def test_rollback_on_real_database_failure(conn, stocked):
    """Audit insert violates the users FK, after stock and transaction rows were written."""
    ghost = AuthenticatedUser(id=uuid.uuid4(), email="ghost@test.local", full_name="Ghost", role=Role.INVENTORY_MANAGER)
    before_qty = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    before = counts(conn)
    with pytest.raises(ConflictError) as excinfo:
        ops.record_stock_movement(
            conn, ghost, item=stocked["item_id"], location=stocked["location_id"], direction="inbound", quantity=7
        )
    assert "agent_audit_log" not in str(excinfo.value) and "INSERT" not in str(excinfo.value)
    assert stock_qty(conn, stocked["item_id"], stocked["location_id"]) == before_qty
    assert counts(conn) == before


def test_rollback_on_unexpected_failure(conn, inv_user, stocked, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*_args, **_kwargs):
        raise OperationalError("INSERT INTO agent_audit_log ...", {}, Exception("connection lost"))

    monkeypatch.setattr(audit_repo, "insert_audit_record", boom)
    before_qty = stock_qty(conn, stocked["item_id"], stocked["location_id"])
    before = counts(conn)
    with pytest.raises(OperationFailedError) as excinfo:
        ops.record_stock_movement(
            conn, inv_user, item=stocked["item_id"], location=stocked["location_id"], direction="outbound", quantity=1
        )
    assert "INSERT" not in str(excinfo.value) and "connection lost" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert stock_qty(conn, stocked["item_id"], stocked["location_id"]) == before_qty
    assert counts(conn) == before


@pytest.mark.parametrize("payload", ["'; DROP TABLE inv_items; --", "x' OR '1'='1", "%", "1; SELECT pg_sleep(5)"])
def test_injection_strings_are_plain_values(conn, inv_user, payload):
    with pytest.raises(NotFoundError):
        ops.get_item_details(conn, inv_user, payload)
    ops.search_items(conn, inv_user, payload)
    assert scalar(conn, "SELECT count(*) FROM inv_items") > 0
