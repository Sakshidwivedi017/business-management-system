"""Procurement operations against the live database. Every test runs in a rolled-back transaction."""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.auth.permissions import AuthenticatedUser, PermissionDenied, Role
from app.db.connection import get_engine
from app.db.repositories import procurement as proc_repo
from app.services import ConflictError, NotFoundError, OperationFailedError, ValidationError
from app.services import procurement as ops
from app.services.procurement import PurchaseOrderLineInput, calculate_line_amounts


def one(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().first()


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


def counts(conn):
    return {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in ("proc_purchase_orders", "proc_po_lines", "agent_audit_log")}


def max_sequence(conn, year):
    return scalar(
        conn,
        "SELECT COALESCE(MAX(CAST(substring(po_number FROM 9) AS integer)), 0) FROM proc_purchase_orders"
        " WHERE po_number LIKE :p",
        p=f"PO-{year}-%",
    )


@pytest.fixture
def pm(demo_users):
    return demo_users["procurement_manager"]


@pytest.fixture
def vendor_id(conn):
    return scalar(conn, "SELECT id FROM proc_vendors WHERE is_active ORDER BY name LIMIT 1")


@pytest.fixture
def items(conn):
    return conn.execute(
        text("SELECT id, item_code, unit FROM inv_items WHERE status = 'active' ORDER BY item_code LIMIT 2")
    ).mappings().all()


def valid_lines(items):
    return [
        {"item": items[0]["item_code"], "quantity": "1.5", "unit_price": "0.67", "tax_percentage": 18},
        PurchaseOrderLineInput(item=items[1]["id"], quantity="2.5", unit_price=0.1, tax_percentage="5", notes="n"),
    ]


# --- calculations ------------------------------------------------------------


def test_line_calculation_rounds_half_up_with_exact_decimals():
    # 3 x 0.335 = 1.005 -> 1.01 (banker's rounding would give 1.00)
    assert calculate_line_amounts(Decimal("3"), Decimal("0.34"), Decimal("18")) == {
        "line_subtotal": Decimal("1.02"), "line_tax": Decimal("0.18"), "line_total": Decimal("1.20"),
    }
    assert calculate_line_amounts(Decimal("3"), Decimal("0.335"), Decimal("0"))["line_subtotal"] == Decimal("1.01")
    # 10.05 x 5% = 0.5025 -> 0.50; 0.1 x 3 stays exactly 0.30 (no float drift)
    assert calculate_line_amounts(Decimal("1"), Decimal("10.05"), Decimal("5"))["line_tax"] == Decimal("0.50")
    assert calculate_line_amounts(Decimal("3"), Decimal("0.1"), Decimal("0"))["line_total"] == Decimal("0.30")


def test_calculation_matches_existing_rows(conn):
    """The same rule reproduces every existing line in the database."""
    for line in conn.execute(text("SELECT * FROM proc_po_lines")).mappings():
        amounts = calculate_line_amounts(line["quantity_ordered"], line["unit_price"], line["tax_percentage"])
        assert amounts == {k: line[k] for k in ("line_subtotal", "line_tax", "line_total")}


# --- reads -------------------------------------------------------------------


def test_vendor_lookup(conn, pm, vendor_id):
    vendors = ops.list_vendors(conn, pm, active_only=True, limit=200)
    assert vendor_id in {v["id"] for v in vendors}
    assert ops.get_vendor(conn, pm, vendor_id)["id"] == vendor_id
    with pytest.raises(NotFoundError):
        ops.get_vendor(conn, pm, "no-such-vendor")


def test_purchase_order_lookup(conn, demo_users):
    owner = demo_users["owner"]
    po = one(conn, "SELECT id, po_number FROM proc_purchase_orders WHERE status = 'received' LIMIT 1")
    by_id = ops.get_purchase_order(conn, owner, po["id"])
    by_number = ops.get_purchase_order(conn, owner, po["po_number"])
    assert by_id == by_number
    assert by_id["purchase_order"]["po_number"] == po["po_number"]
    assert len(by_id["lines"]) == scalar(conn, "SELECT count(*) FROM proc_po_lines WHERE po_id = :p", p=po["id"])
    assert set(by_id) == {"purchase_order", "lines", "receipts", "payment_tranches"}
    with pytest.raises(NotFoundError):
        ops.get_purchase_order(conn, owner, "PO-1999-0001")


def test_list_purchase_orders_filters(conn, pm):
    placed = ops.list_purchase_orders(conn, pm, status="placed", limit=200)
    assert placed and all(p["status"] == "placed" for p in placed)
    assert len(placed) == scalar(conn, "SELECT count(*) FROM proc_purchase_orders WHERE status = 'placed'")
    with pytest.raises(ValidationError):
        ops.list_purchase_orders(conn, pm, status="cancelled")


# --- create_purchase_order ---------------------------------------------------


def test_create_purchase_order(conn, pm, vendor_id, items):
    year = datetime.now(UTC).year
    expected_seq = max_sequence(conn, year) + 1
    result = ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items))

    header = result["purchase_order"]
    assert re.fullmatch(r"PO-\d{4}-\d{4}", header["po_number"])
    assert header["po_number"] == f"PO-{year}-{expected_seq:04d}"
    assert header["status"] == "placed"
    assert header["placed_on"] == datetime.now(UTC).date()
    assert header["vendor_id"] == vendor_id and header["currency"] == "INR"
    assert header["created_by"] == str(pm.id)

    lines = result["lines"]
    assert [line["position"] for line in lines] == [0, 1]
    assert [line["inv_item_id"] for line in lines] == [items[0]["id"], items[1]["id"]]
    assert [line["unit"] for line in lines] == [items[0]["unit"], items[1]["unit"]]
    assert all(line["quantity_received"] == 0 for line in lines)
    # Line 0: 1.5 x 0.67 = 1.005 -> 1.01, tax 18% = 0.1818 -> 0.18. Line 1: 2.5 x 0.10 = 0.25, tax 5% = 0.0125 -> 0.01.
    assert [(l["line_subtotal"], l["line_tax"], l["line_total"]) for l in lines] == [
        (Decimal("1.01"), Decimal("0.18"), Decimal("1.19")),
        (Decimal("0.25"), Decimal("0.01"), Decimal("0.26")),
    ]
    assert (header["subtotal"], header["tax_amount"], header["total_amount"]) == (
        Decimal("1.26"), Decimal("0.19"), Decimal("1.45"),
    )

    audit = one(conn, "SELECT * FROM agent_audit_log WHERE operation_id = :o", o=result["audit_operation_id"])
    assert audit["user_id"] == pm.id and audit["role"] == "procurement_manager"
    assert audit["operation"] == "create_purchase_order" and audit["entity_type"] == "purchase_order"
    assert audit["entity_id"] == header["id"] and audit["status"] == "success" and audit["thread_id"] is None
    assert audit["after_state"]["po_number"] == header["po_number"]
    assert audit["after_state"]["total_amount"] == "1.45"


def test_caller_supplied_totals_are_rejected(conn, pm, vendor_id, items):
    line = {"item": items[0]["id"], "quantity": 1, "unit_price": 10, "tax_percentage": 0, "line_total": 1}
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=[line])
    with pytest.raises(TypeError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=[line], total_amount=1)


def test_po_numbers_are_sequential_within_a_transaction(conn, pm, vendor_id, items):
    first = ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items))
    second = ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items))
    n1 = int(first["purchase_order"]["po_number"][-4:])
    n2 = int(second["purchase_order"]["po_number"][-4:])
    assert n2 == n1 + 1


def test_po_numbering_lock_blocks_concurrent_creator(live_db, demo_users, items):
    """While one transaction holds the numbering lock, another cannot allocate a number."""
    pm = demo_users["procurement_manager"]
    engine = get_engine()
    with engine.connect() as conn_a, engine.connect() as conn_b:
        tx_a = conn_a.begin()
        tx_b = conn_b.begin()
        try:
            vendor = scalar(conn_a, "SELECT id FROM proc_vendors WHERE is_active ORDER BY name LIMIT 1")
            created = ops.create_purchase_order(conn_a, pm, vendor_id=vendor, lines=valid_lines(items))

            conn_b.execute(text("SET LOCAL lock_timeout = '500ms'"))
            with pytest.raises(OperationFailedError):
                ops.create_purchase_order(conn_b, pm, vendor_id=vendor, lines=valid_lines(items))

            # Once A ends, B proceeds and allocates the next free number itself.
            tx_a.rollback()
            retried = ops.create_purchase_order(conn_b, pm, vendor_id=vendor, lines=valid_lines(items))
            assert retried["purchase_order"]["po_number"] == created["purchase_order"]["po_number"]
        finally:
            if tx_a.is_active:
                tx_a.rollback()
            tx_b.rollback()


def test_duplicate_po_number_becomes_conflict(conn, pm, vendor_id, items, monkeypatch):
    existing = scalar(conn, "SELECT max(po_number) FROM proc_purchase_orders")
    year, seq = int(existing[3:7]), int(existing[-4:])
    monkeypatch.setattr(proc_repo, "get_max_po_sequence", lambda _c, _y: seq - 1)
    monkeypatch.setattr(ops, "utcnow", lambda: datetime(year, 6, 1))
    before = counts(conn)
    with pytest.raises(ConflictError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items))
    assert counts(conn) == before


def test_delivery_location(conn, pm, vendor_id, items):
    loc = one(conn, "SELECT id, name FROM inv_locations WHERE is_active LIMIT 1")
    result = ops.create_purchase_order(
        conn, pm, vendor_id=vendor_id, lines=valid_lines(items), delivery_location=loc["name"]
    )
    assert result["purchase_order"]["delivery_location_id"] == loc["id"]
    inactive = scalar(conn, "SELECT id FROM inv_locations WHERE NOT is_active LIMIT 1")
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items), delivery_location=inactive)
    with pytest.raises(NotFoundError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items), delivery_location="nowhere")


def test_invalid_vendor(conn, pm, items, monkeypatch):
    with pytest.raises(NotFoundError):
        ops.create_purchase_order(conn, pm, vendor_id="no-such-vendor", lines=valid_lines(items))
    # No inactive vendor exists in the data; simulate one without touching the database.
    real = proc_repo.get_vendor_by_id
    monkeypatch.setattr(proc_repo, "get_vendor_by_id", lambda c, v: {**real(c, v), "is_active": False})
    vendor = scalar(conn, "SELECT id FROM proc_vendors LIMIT 1")
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor, lines=valid_lines(items))


def test_invalid_item(conn, pm, vendor_id, items):
    lines = valid_lines(items) + [{"item": "NO-SUCH-ITEM", "quantity": 1, "unit_price": 1, "tax_percentage": 0}]
    before = counts(conn)
    with pytest.raises(NotFoundError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=lines)
    inactive = scalar(conn, "SELECT id FROM inv_items WHERE status <> 'active' LIMIT 1")
    with pytest.raises(ValidationError):
        ops.create_purchase_order(
            conn, pm, vendor_id=vendor_id,
            lines=[{"item": inactive, "quantity": 1, "unit_price": 1, "tax_percentage": 0}],
        )
    assert counts(conn) == before


def _line(items, **overrides):
    return [{"item": items[0]["id"], "quantity": 1, "unit_price": 1, "tax_percentage": 0, **overrides}]


@pytest.mark.parametrize("quantity", [0, -1, "x", None, True, "1.0001", float("nan"), Decimal("1e12")])
def test_invalid_quantity(conn, pm, vendor_id, items, quantity):
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=_line(items, quantity=quantity))


@pytest.mark.parametrize("unit_price", [-0.01, "abc", None, "1.001", float("inf"), Decimal("1e13")])
def test_invalid_price(conn, pm, vendor_id, items, unit_price):
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=_line(items, unit_price=unit_price))


def test_zero_price_allowed(conn, pm, vendor_id, items):
    result = ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=_line(items, unit_price=0))
    assert result["purchase_order"]["total_amount"] == 0


@pytest.mark.parametrize("tax", [-1, "100.01", 101, "abc", None, "5.001"])
def test_invalid_tax(conn, pm, vendor_id, items, tax):
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=_line(items, tax_percentage=tax))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lines": []},
        {"lines": "not a list"},
        {"lines": {"item": "x"}},
        {"lines": [{"item": "x"}]},
        {"lines": ["junk"]},
        {"lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 0}] * 51},
        {"currency": "inr"},
        {"currency": "RUPEES"},
        {"vendor_id": ""},
    ],
    ids=["empty", "string", "mapping", "missing-fields", "junk-line", "too-many", "lower-ccy", "long-ccy", "no-vendor"],
)
def test_invalid_request_shape(conn, pm, vendor_id, items, kwargs):
    args = {"vendor_id": vendor_id, "lines": _line(items), **kwargs}
    with pytest.raises(ValidationError):
        ops.create_purchase_order(conn, pm, **args)


@pytest.mark.parametrize("role", ["inventory_manager", "owner"])
def test_unauthorized_po_creation(conn, demo_users, vendor_id, items, role):
    before = counts(conn)
    with pytest.raises(PermissionDenied):
        ops.create_purchase_order(conn, demo_users[role], vendor_id=vendor_id, lines=valid_lines(items))
    assert counts(conn) == before


def test_rollback_leaves_no_header_or_lines(conn, vendor_id, items):
    """Header and both lines are inserted, then the audit insert violates the users FK."""
    ghost = AuthenticatedUser(id=uuid.uuid4(), email="g@test.local", full_name="G", role=Role.PROCUREMENT_MANAGER)
    before = counts(conn)
    with pytest.raises(ConflictError):
        ops.create_purchase_order(conn, ghost, vendor_id=vendor_id, lines=valid_lines(items))
    assert counts(conn) == before


def test_failure_mid_lines_rolls_back(conn, pm, vendor_id, items, monkeypatch):
    from sqlalchemy.exc import OperationalError

    real_insert = proc_repo.insert_po_line
    calls = []

    def fail_on_second(conn_, **kwargs):
        calls.append(kwargs["position"])
        if kwargs["position"] == 1:
            raise OperationalError("INSERT INTO proc_po_lines", {}, Exception("boom"))
        real_insert(conn_, **kwargs)

    monkeypatch.setattr(proc_repo, "insert_po_line", fail_on_second)
    before = counts(conn)
    with pytest.raises(OperationFailedError):
        ops.create_purchase_order(conn, pm, vendor_id=vendor_id, lines=valid_lines(items))
    assert calls == [0, 1]
    assert counts(conn) == before
