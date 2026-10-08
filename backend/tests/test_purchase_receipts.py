"""Goods receipts against purchase orders: the Layer 4 operation and its Layer 8 confirmation flow.

Live tests run in the rolled-back `conn` transaction and receive against purchase orders they create
there, so real orders, stock and receipts are never changed. The lock test uses two connections whose
transactions are both rolled back.
"""

import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agent import mutations
from app.agent.graph import Agent, thread_id_for
from app.agent.mutations import MutationState
from app.agent.tools import MUTATION_TOOLS
from app.auth.permissions import PermissionDenied, Role
from app.db.connection import get_engine
from app.db.repositories import procurement as proc_repo
from app.services import NotFoundError, OperationFailedError, ValidationError
from app.services import procurement as ops
from tests.fakes import ScriptedChatModel, make_agent_user, tool_call

RECEIPT = "record_purchase_receipt"
PRICE = "10.50"


def one(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().first()


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


@pytest.fixture
def pm(demo_users):
    return demo_users["procurement_manager"]


@pytest.fixture
def setup(conn):
    items = conn.execute(
        text("SELECT id, item_code, name FROM inv_items WHERE status = 'active' ORDER BY item_code LIMIT 2")
    ).mappings().all()
    return SimpleNamespace(
        vendor=scalar(conn, "SELECT id FROM proc_vendors WHERE is_active ORDER BY name LIMIT 1"),
        a=items[0],
        b=items[1],
        location=one(conn, "SELECT id, name FROM inv_locations WHERE is_active ORDER BY name LIMIT 1"),
    )


def new_po(conn, pm, setup, *lines):
    """A placed PO created inside the test's transaction: lines are (item, quantity ordered)."""
    created = ops.create_purchase_order(
        conn, pm, vendor_id=setup.vendor,
        lines=[{"item": item["item_code"], "quantity": qty, "unit_price": PRICE, "tax_percentage": 18}
               for item, qty in lines],
    )
    return created["purchase_order"]


def line(po, item, quantity, position=None):
    entry = {"purchase_order": po["po_number"], "item": item["item_code"], "quantity": quantity}
    return entry if position is None else {**entry, "line_position": position}


def stock_at(conn, item_id, location_id):
    value = conn.execute(
        text("SELECT quantity FROM inv_current_stock WHERE item_id = :i AND location_id = :l"),
        {"i": item_id, "l": location_id},
    ).scalar()
    return Decimal(str(value)) if value is not None else Decimal(0)


def received(conn, po):
    return [r["quantity_received"] for r in conn.execute(
        text("SELECT quantity_received FROM proc_po_lines WHERE po_id = :p ORDER BY position"), {"p": po["id"]}
    ).mappings()]


def status(conn, po):
    return scalar(conn, "SELECT status FROM proc_purchase_orders WHERE id = :p", p=po["id"])


def snapshot(conn, setup, *pos):
    """Everything a receipt can change, for the given orders and the setup's items at its location."""
    return {
        "receipts": scalar(conn, "SELECT count(*) FROM proc_po_receipts"),
        "transactions": scalar(conn, "SELECT count(*) FROM inv_transactions"),
        "layer4_audit": scalar(conn, "SELECT count(*) FROM agent_audit_log WHERE operation NOT LIKE :p", p="%:request"),
        "lines": [received(conn, po) for po in pos],
        "statuses": [status(conn, po) for po in pos],
        "stock": [stock_at(conn, item["id"], setup.location["id"]) for item in (setup.a, setup.b)],
    }


def receive(conn, user, setup, *lines, **kwargs):
    return ops.record_purchase_receipt(conn, user, location=setup.location["name"], lines=list(lines), **kwargs)


# --- permission and input (no database) ------------------------------------------------------


@pytest.mark.parametrize("role", [Role.INVENTORY_MANAGER, Role.OWNER])
def test_receipt_denied_without_procurement_write(role):
    with pytest.raises(PermissionDenied):
        ops.record_purchase_receipt(None, make_agent_user(role), location="103-1",
                                    lines=[{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 6}])


@pytest.mark.parametrize(
    "lines",
    [
        [],
        "PO-2026-0057",
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 0}],
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": -1}],
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": "1.0001"}],
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 1, "line_position": -1}],
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 1, "line_position": True}],
        [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 1, "unit_price": 1}],
        [{"purchase_order": "", "item": "BO-MC-0172", "quantity": 1}],
        [{"purchase_order": "PO-2026-0057", "quantity": 1}],
    ],
)
def test_receipt_input_validated_before_database(lines):
    with pytest.raises(ValidationError):
        ops.record_purchase_receipt(None, make_agent_user(Role.PROCUREMENT_MANAGER), location="103-1", lines=lines)


# --- Layer 4: what a receipt writes ------------------------------------------------------------


def test_receipt_records_stock_transaction_receipt_line_status_and_audit(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10), (setup.b, 4))
    stock_before = [stock_at(conn, item["id"], setup.location["id"]) for item in (setup.a, setup.b)]

    result = receive(conn, pm, setup, line(po, setup.a, 6), line(po, setup.b, 4), notes="invoice no 1")

    assert [stock_at(conn, item["id"], setup.location["id"]) for item in (setup.a, setup.b)] == [
        stock_before[0] + 6, stock_before[1] + 4
    ]
    assert received(conn, po) == [Decimal(6), Decimal(4)] and status(conn, po) == "partial"
    assert result["purchase_orders"] == [
        {"po_id": po["id"], "po_number": po["po_number"], "status_before": "placed", "status_after": "partial"}
    ]
    for entry, quantity in zip(result["receipts"], (6, 4), strict=True):
        receipt = one(conn, "SELECT * FROM proc_po_receipts WHERE id = :id", id=entry["receipt_id"])
        txn = one(conn, "SELECT * FROM inv_transactions WHERE id = :id", id=receipt["inv_transaction_id"])
        # The historical convention: one inbound transaction per receipt, referencing the PO.
        assert receipt["po_line_id"] == entry["po_line_id"] and receipt["quantity"] == quantity
        assert receipt["location_id"] == setup.location["id"] and receipt["received_by"] == str(pm.id)
        assert receipt["notes"] == txn["notes"] == "invoice no 1"
        assert (txn["transaction_type"], txn["quantity"], txn["item_id"]) == ("inbound", quantity, entry["item_id"])
        assert (txn["reference_type"], txn["reference_id"]) == ("purchase_order", po["id"])
        assert (txn["unit_cost"], txn["currency"]) == (float(PRICE), po["currency"])  # from the PO, never input
        assert txn["location_id"] == setup.location["id"] and txn["created_by"] == str(pm.id)
        assert txn["created_at"] == receipt["received_at"]

    audit = one(conn, "SELECT * FROM agent_audit_log WHERE operation_id = :id", id=result["audit_operation_id"])
    assert (audit["operation"], audit["entity_type"], audit["status"]) == (RECEIPT, "purchase_receipt", "success")
    assert audit["user_id"] == pm.id and audit["role"] == "procurement_manager"
    assert audit["entity_id"] == result["receipts"][0]["receipt_id"]
    assert [r["receipt_id"] for r in audit["after_state"]["receipts"]] == [r["receipt_id"] for r in result["receipts"]]


def test_status_moves_placed_partial_received_and_then_refuses_more(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10), (setup.b, 4))
    assert status(conn, po) == "placed"
    receive(conn, pm, setup, line(po, setup.a, 6))
    assert status(conn, po) == "partial"
    result = receive(conn, pm, setup, line(po, setup.a, "4"), line(po, setup.b, 4))
    assert status(conn, po) == "received" and result["purchase_orders"][0]["status_before"] == "partial"
    assert received(conn, po) == [Decimal(10), Decimal(4)]
    before = snapshot(conn, setup, po)
    with pytest.raises(ValidationError, match="already fully received"):
        receive(conn, pm, setup, line(po, setup.a, 1))
    assert snapshot(conn, setup, po) == before


def test_fully_received_line_is_refused(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10), (setup.b, 4))
    receive(conn, pm, setup, line(po, setup.b, 4))
    before = snapshot(conn, setup, po)
    with pytest.raises(ValidationError, match=f"'{setup.b['item_code']}' on purchase order .* already fully received"):
        receive(conn, pm, setup, line(po, setup.b, 1))
    assert snapshot(conn, setup, po) == before


def test_over_receipt_is_refused_without_changes(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10))
    receive(conn, pm, setup, line(po, setup.a, 7))
    before = snapshot(conn, setup, po)
    with pytest.raises(ValidationError, match="only 3 .*remain"):
        receive(conn, pm, setup, line(po, setup.a, "3.001"))
    assert snapshot(conn, setup, po) == before


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        (lambda e, s: {**e, "purchase_order": "PO-1999-9999"}, NotFoundError, "Purchase order"),
        (lambda e, s: {**e, "item": "NO-SUCH-ITEM"}, NotFoundError, "Item 'NO-SUCH-ITEM' not found"),
        (lambda e, s: {**e, "item": s.b["item_code"]}, NotFoundError, "is not on purchase order"),
        (lambda e, s: {**e, "line_position": 7}, NotFoundError, "at line position 7"),
    ],
)
def test_unknown_or_mismatched_lines_are_refused(conn, pm, setup, change, error, message):
    po = new_po(conn, pm, setup, (setup.a, 10))
    before = snapshot(conn, setup, po)
    with pytest.raises(error, match=message):
        receive(conn, pm, setup, change(line(po, setup.a, 1), setup))
    assert snapshot(conn, setup, po) == before


def test_unknown_and_inactive_locations_are_refused(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10))
    before = snapshot(conn, setup, po)
    with pytest.raises(NotFoundError):
        ops.record_purchase_receipt(conn, pm, location="No Such Warehouse", lines=[line(po, setup.a, 1)])
    inactive = scalar(conn, "SELECT name FROM inv_locations WHERE NOT is_active ORDER BY name LIMIT 1")
    with pytest.raises(ValidationError, match="is not active"):
        ops.record_purchase_receipt(conn, pm, location=inactive, lines=[line(po, setup.a, 1)])
    assert snapshot(conn, setup, po) == before


def test_item_on_several_lines_needs_its_line_position(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10), (setup.b, 4), (setup.a, 2))
    before = snapshot(conn, setup, po)
    with pytest.raises(ValidationError, match="line positions 0, 2"):
        receive(conn, pm, setup, line(po, setup.a, 1))
    assert snapshot(conn, setup, po) == before
    receive(conn, pm, setup, line(po, setup.a, 2, position=2))
    assert received(conn, po) == [Decimal(0), Decimal(0), Decimal(2)] and status(conn, po) == "partial"


def test_same_line_twice_in_one_receipt_is_refused(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10))
    before = snapshot(conn, setup, po)
    with pytest.raises(ValidationError, match="listed more than once"):
        receive(conn, pm, setup, line(po, setup.a, 1), line(po, setup.a, 2))
    assert snapshot(conn, setup, po) == before


def test_several_purchase_orders_in_one_receipt(conn, pm, setup):
    # "We received 6 pcs for one PO and 3 pcs for another": one location, one stock row, both orders.
    first, second = new_po(conn, pm, setup, (setup.a, 10)), new_po(conn, pm, setup, (setup.a, 3))
    start = stock_at(conn, setup.a["id"], setup.location["id"])
    result = receive(conn, pm, setup, line(first, setup.a, 6), line(second, setup.a, 3))
    assert stock_at(conn, setup.a["id"], setup.location["id"]) == start + 9
    assert [(r["stock_before"], r["stock_after"]) for r in result["receipts"]] == [(start, start + 6),
                                                                                   (start + 6, start + 9)]
    assert (status(conn, first), status(conn, second)) == ("partial", "received")
    assert [p["po_number"] for p in result["purchase_orders"]] == [first["po_number"], second["po_number"]]


def test_one_invalid_line_rejects_the_whole_receipt(conn, pm, setup):
    first, second = new_po(conn, pm, setup, (setup.a, 10)), new_po(conn, pm, setup, (setup.b, 3))
    before = snapshot(conn, setup, first, second)
    with pytest.raises(ValidationError, match="only 3 .*remain"):
        receive(conn, pm, setup, line(first, setup.a, 6), line(second, setup.b, 4))
    assert snapshot(conn, setup, first, second) == before


def test_failure_while_writing_rolls_back_everything(conn, pm, setup, monkeypatch):
    po = new_po(conn, pm, setup, (setup.a, 10), (setup.b, 4))
    before = snapshot(conn, setup, po)

    def fail(*args, **kwargs):  # the last write before the audit record
        raise OperationalError("UPDATE proc_purchase_orders", {}, Exception("connection lost"))

    monkeypatch.setattr(proc_repo, "update_purchase_order_status", fail)
    with pytest.raises(OperationFailedError):
        receive(conn, pm, setup, line(po, setup.a, 6), line(po, setup.b, 4))
    assert snapshot(conn, setup, po) == before


def test_po_lines_are_locked_against_a_concurrent_receipt(live_db, demo_users):
    """While one receipt is uncommitted, another against the same PO waits; both are rolled back."""
    pm = demo_users["procurement_manager"]
    engine = get_engine()
    with engine.connect() as conn_a, engine.connect() as conn_b:
        tx_a, tx_b = conn_a.begin(), conn_b.begin()
        try:
            open_line = one(
                conn_a,
                """
                SELECT po.po_number, i.item_code, pl.position
                FROM proc_po_lines pl JOIN proc_purchase_orders po ON po.id = pl.po_id
                JOIN inv_items i ON i.id = pl.inv_item_id
                WHERE po.status <> 'received' AND pl.quantity_ordered - pl.quantity_received >= 1
                ORDER BY po.po_number, pl.position LIMIT 1
                """,
            )
            location = scalar(conn_a, "SELECT name FROM inv_locations WHERE is_active ORDER BY name LIMIT 1")
            entry = {"purchase_order": open_line["po_number"], "item": open_line["item_code"], "quantity": 1,
                     "line_position": open_line["position"]}
            ops.record_purchase_receipt(conn_a, pm, location=location, lines=[entry])

            conn_b.execute(text("SET LOCAL lock_timeout = '500ms'"))
            with pytest.raises(OperationFailedError):
                ops.record_purchase_receipt(conn_b, pm, location=location, lines=[entry])
        finally:
            tx_a.rollback()
            tx_b.rollback()


# --- Layer 8: proposal, confirmation, idempotency -----------------------------------------------


@pytest.fixture
def db(conn, monkeypatch):
    """Layer 8 on the test's rolled-back connection; its transactions become savepoints."""

    class Shared:
        @contextmanager
        def connect(self):
            yield conn

    monkeypatch.setattr(mutations, "get_engine", lambda: Shared())
    return conn


def arguments(setup, *lines):
    """Arguments exactly as the executor hands them to Layer 8: validated, then dumped."""
    return MUTATION_TOOLS[RECEIPT].args_schema.model_validate(
        {"location": setup.location["name"], "lines": list(lines)}
    ).model_dump()


def request(conn, proposal_id):
    return one(conn, "SELECT * FROM agent_audit_log WHERE operation_id = :id", id=proposal_id)


def test_proposal_is_a_dry_run_audited_as_pending(db, pm, setup):
    po = new_po(db, pm, setup, (setup.a, 10), (setup.b, 4))
    before = snapshot(db, setup, po)
    proposal = mutations.propose(pm, thread_id_for(pm, uuid.uuid4()), RECEIPT,
                                 arguments(setup, line(po, setup.a, 6), line(po, setup.b, 4)))

    assert proposal.state is MutationState.CONFIRMATION_REQUIRED and snapshot(db, setup, po) == before
    for expected in (po["po_number"], setup.a["item_code"], setup.b["item_code"], f"at {setup.location['name']}",
                     "received 0 -> 6 of 10", "would become partial", "Nothing has been changed yet"):
        assert expected in proposal.message
    assert PRICE not in proposal.message and "10.5" not in proposal.message
    assert "receipt_id" not in proposal.details and "unit_price" not in str(proposal.details)
    row = request(db, proposal.proposal_id)
    assert (row["operation"], row["status"], row["user_id"]) == (f"{RECEIPT}:request", "pending", pm.id)


def test_confirmation_executes_once_and_repeats_are_idempotent(db, pm, setup):
    po = new_po(db, pm, setup, (setup.a, 10))
    thread = thread_id_for(pm, uuid.uuid4())
    proposal = mutations.propose(pm, thread, RECEIPT, arguments(setup, line(po, setup.a, 6)))
    start = snapshot(db, setup, po)

    done = mutations.confirm_proposal(pm, thread, proposal.proposal_id)
    assert done.state is MutationState.EXECUTED and done.message.startswith("Done: recorded goods received")
    assert scalar(db, "SELECT count(*) FROM proc_po_receipts WHERE id = :id", id=done.entity_id) == 1
    after = snapshot(db, setup, po)
    assert after["receipts"] == start["receipts"] + 1 and after["transactions"] == start["transactions"] + 1
    assert after["lines"] == [[Decimal(6)]] and after["statuses"] == ["partial"]
    assert after["stock"][0] == start["stock"][0] + 6

    again = mutations.confirm_proposal(pm, thread, proposal.proposal_id)
    assert again.state is MutationState.ALREADY_EXECUTED and snapshot(db, setup, po) == after
    row = request(db, proposal.proposal_id)
    assert row["status"] == "success" and row["entity_id"] == done.entity_id
    assert row["after_state"]["duplicate_confirmations"] == 1


def test_cancelled_receipt_changes_nothing(db, pm, setup):
    po = new_po(db, pm, setup, (setup.a, 10))
    thread = thread_id_for(pm, uuid.uuid4())
    proposal = mutations.propose(pm, thread, RECEIPT, arguments(setup, line(po, setup.a, 6)))
    before = snapshot(db, setup, po)
    assert mutations.cancel_proposal(pm, thread, proposal.proposal_id).state is MutationState.CANCELLED
    assert mutations.confirm_proposal(pm, thread, proposal.proposal_id).state is MutationState.NOT_PENDING
    assert snapshot(db, setup, po) == before
    assert request(db, proposal.proposal_id)["after_state"] == {"outcome": "cancelled"}


def test_confirmation_rechecks_what_remains(db, pm, setup):
    po = new_po(db, pm, setup, (setup.a, 10))
    thread = thread_id_for(pm, uuid.uuid4())
    proposal = mutations.propose(pm, thread, RECEIPT, arguments(setup, line(po, setup.a, 6)))
    receive(db, pm, setup, line(po, setup.a, 5))  # received elsewhere before the confirmation
    before = snapshot(db, setup, po)
    failed = mutations.confirm_proposal(pm, thread, proposal.proposal_id)
    assert failed.state is MutationState.FAILED and "only 5" in failed.message
    assert snapshot(db, setup, po) == before and request(db, proposal.proposal_id)["status"] == "failed"


@pytest.mark.parametrize("role", ["inventory_manager", "owner"])
def test_roles_without_procurement_write_cannot_propose(db, pm, demo_users, setup, role):
    po = new_po(db, pm, setup, (setup.a, 10))
    user = demo_users[role]
    before = scalar(db, "SELECT count(*) FROM agent_audit_log")
    with pytest.raises(PermissionDenied):
        mutations.propose(user, thread_id_for(user, uuid.uuid4()), RECEIPT, arguments(setup, line(po, setup.a, 6)))
    assert scalar(db, "SELECT count(*) FROM agent_audit_log") == before


def test_chat_turns_propose_one_receipt_for_several_orders_and_confirm_it(db, pm, setup):
    first, second = new_po(db, pm, setup, (setup.a, 10)), new_po(db, pm, setup, (setup.a, 3))
    call = tool_call(RECEIPT, {"location": setup.location["name"],
                               "lines": [line(first, setup.a, 6), line(second, setup.a, 3)]})
    model = ScriptedChatModel(responses=[call])
    agent = Agent(model, InMemorySaver(), 12)
    conversation = uuid.uuid4()
    start = snapshot(db, setup, first, second)

    proposed = agent.respond(pm, conversation, f"We received 6 pcs for {first['po_number']} and 3 pcs for "
                                               f"{second['po_number']} at {setup.location['name']}")
    assert proposed.mutation.state is MutationState.CONFIRMATION_REQUIRED and proposed.mutation.operation == RECEIPT
    assert snapshot(db, setup, first, second) == start

    done = agent.respond(pm, conversation, "confirm")
    assert done.mutation.state is MutationState.EXECUTED and len(model.calls) == 1
    end = snapshot(db, setup, first, second)
    assert end["receipts"] == start["receipts"] + 2 and end["statuses"] == ["partial", "received"]
    assert end["stock"][0] == start["stock"][0] + 9
    assert done.text == done.mutation.message  # Layer 8 wrote the reply, not the model


# --- mistyped references in a change ----------------------------------------------------------------------------


def test_mistyped_purchase_order_is_suggested_and_nothing_is_received(conn, pm, setup):
    po = new_po(conn, pm, setup, (setup.a, 10))
    typo = po["po_number"][:-4] + po["po_number"][-3:]  # PO-2026-0059 -> PO-2026-059
    before = snapshot(conn, setup, po)
    with pytest.raises(NotFoundError) as caught:
        receive(conn, pm, setup, {"purchase_order": typo, "item": setup.a["item_code"], "quantity": 1})
    assert f"Did you mean {po['po_number']}?" in str(caught.value) and "Confirm with the user" in str(caught.value)
    assert snapshot(conn, setup, po) == before


def test_case_only_differences_resolve_and_the_preview_names_the_real_records(db, pm, setup):
    po = new_po(db, pm, setup, (setup.a, 10))
    proposal = mutations.propose(pm, thread_id_for(pm, uuid.uuid4()), RECEIPT, arguments(
        setup, {"purchase_order": po["po_number"].lower(), "item": setup.a["item_code"].lower(), "quantity": 2}
    ))
    # The user confirms what the system resolved, shown exactly as stored.
    assert f"- {po['po_number']}: 2 " in proposal.message and setup.a["item_code"] in proposal.message
