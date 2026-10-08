"""Layer 8: mutation proposal, explicit confirmation, idempotent execution and audit.

Live-database tests run on the rolled-back `conn` fixture: Layer 8's transactions become
savepoints inside it. The concurrency and connection tests must commit, so they use
operations that touch no business table and delete their own audit rows afterwards.
"""

import dataclasses
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agent import graph as agent_graph
from app.agent import mutations
from app.agent import tools as agent_tools
from app.agent.graph import Agent, thread_id_for
from app.agent.mutations import MUTATIONS, PROPOSAL_TTL_SECONDS, MutationState, MutationStatus, awaited_proposal, read_decision
from app.agent.tools import MUTATION_TOOLS, TOOLS, available_tools, execute_tool
from app.auth.permissions import AuthenticatedUser, Permission, Role
from app.config import get_settings
from app.db.connection import get_engine
from app.db.repositories import audit
from app.services import NotFoundError, inventory, procurement
from app.services.procurement import calculate_line_amounts
from tests.fakes import NoDatabase, ScriptedChatModel, make_agent_user, tool_call

MOVE = "record_stock_movement"
ORDER = "create_purchase_order"
RECEIPT = "record_purchase_receipt"


def args(name, **values):
    """Arguments exactly as the executor hands them to Layer 8: validated, then dumped."""
    return MUTATION_TOOLS[name].args_schema.model_validate(values).model_dump()


def parse(result):
    return json.loads(result.content)


# --- registry and schemas -----------------------------------------------------------


def test_only_the_existing_mutations_are_exposed_each_behind_layer8():
    assert set(MUTATION_TOOLS) == set(MUTATIONS) == {MOVE, ORDER, RECEIPT}
    assert (MUTATION_TOOLS[MOVE].permission, MUTATION_TOOLS[MOVE].operation) == (
        Permission.INVENTORY_WRITE, inventory.record_stock_movement
    )
    assert (MUTATION_TOOLS[ORDER].permission, MUTATION_TOOLS[ORDER].operation) == (
        Permission.PROCUREMENT_WRITE, procurement.create_purchase_order
    )
    assert (MUTATION_TOOLS[RECEIPT].permission, MUTATION_TOOLS[RECEIPT].operation) == (
        Permission.PROCUREMENT_WRITE, procurement.record_purchase_receipt
    )
    for name, tool in MUTATION_TOOLS.items():
        assert tool.mutation and MUTATIONS[name].operation is tool.operation
    assert not any(tool.mutation for tool in TOOLS.values())  # the read registry stays read-only


def test_mutation_schemas_are_strict_and_have_no_identity_fields():
    forbidden = {"user", "user_id", "role", "permission", "created_by", "sql", "conn", "thread_id", "operation_id"}
    for tool in MUTATION_TOOLS.values():
        params = tool.openai_schema()["function"]["parameters"]
        assert params["additionalProperties"] is False and not forbidden & set(params["properties"])
        for definition in params.get("$defs", {}).values():
            assert definition["additionalProperties"] is False and not forbidden & set(definition["properties"])


@pytest.mark.parametrize(
    ("role", "offered"),
    [(Role.INVENTORY_MANAGER, {MOVE}), (Role.PROCUREMENT_MANAGER, {ORDER, RECEIPT}), (Role.OWNER, set())],
)
def test_mutations_offered_by_write_permission(role, offered):
    assert {t.name for t in available_tools(make_agent_user(role))} & {MOVE, ORDER, RECEIPT} == offered


def test_mutation_tool_without_layer8_wiring_is_never_offered_or_run(monkeypatch):
    # A mutation tool pointing at a different operation than Layer 8 executes is refused outright.
    monkeypatch.setitem(MUTATION_TOOLS, MOVE, dataclasses.replace(MUTATION_TOOLS[MOVE], operation=lambda *a, **k: 1))
    monkeypatch.setattr(mutations, "get_engine", lambda: NoDatabase())
    user = make_agent_user(Role.INVENTORY_MANAGER)
    assert MOVE not in {t.name for t in available_tools(user)}
    result = execute_tool(user, MOVE, {"item": "x", "location": "y", "direction": "inbound", "quantity": 1},
                          thread_id="t")
    assert parse(result) == {"error": f"Unknown tool '{MOVE}'"}


# --- executor boundary (no database) -------------------------------------------------------

VALID = {
    MOVE: {"item": "BO-MC-0100", "location": "132-1", "direction": "inbound", "quantity": 2},
    ORDER: {"vendor_id": "v1", "lines": [{"item": "BO-MC-0100", "quantity": 1, "unit_price": 10, "tax_percentage": 18}]},
    RECEIPT: {"location": "103-1", "lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 6},
                                             {"purchase_order": "PO-2026-0056", "item": "BO-MC-0172", "quantity": 3}]},
}


@pytest.fixture
def no_database(monkeypatch):
    monkeypatch.setattr(mutations, "get_engine", lambda: NoDatabase())
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())


@pytest.mark.parametrize(
    ("role", "name"),
    [(Role.OWNER, MOVE), (Role.PROCUREMENT_MANAGER, MOVE), (Role.OWNER, ORDER), (Role.INVENTORY_MANAGER, ORDER),
     (Role.OWNER, RECEIPT), (Role.INVENTORY_MANAGER, RECEIPT)],
)
def test_unauthorized_mutation_denied_before_database(role, name, no_database):
    result = execute_tool(make_agent_user(role), name, VALID[name], thread_id="t")
    assert result.is_error and result.mutation is None
    assert parse(result) == {"error": f"You do not have permission to use {name}"}


@pytest.mark.parametrize("name", [MOVE, ORDER, RECEIPT])
@pytest.mark.parametrize(
    "field", ["user_id", "role", "permission", "created_by", "sql", "conn", "thread_id", "operation_id", "status"]
)
def test_model_cannot_supply_authoritative_fields(name, field, no_database):
    role = Role.INVENTORY_MANAGER if name == MOVE else Role.PROCUREMENT_MANAGER
    result = execute_tool(make_agent_user(role), name, {**VALID[name], field: "x"}, thread_id="t")
    assert parse(result) == {"error": f"Invalid arguments for {name}: {field}: Extra inputs are not permitted"}


@pytest.mark.parametrize(
    "bad",
    [
        {"quantity": "2"},  # strings are not coerced
        {"quantity": 0},
        {"quantity": -5},
        {"direction": "transfer"},
        {"item": ""},
        {"notes": "x" * 501},
    ],
)
def test_malformed_movement_arguments_rejected(bad, no_database):
    result = execute_tool(make_agent_user(Role.INVENTORY_MANAGER), MOVE, {**VALID[MOVE], **bad}, thread_id="t")
    assert result.is_error and parse(result)["error"].startswith(f"Invalid arguments for {MOVE}")


@pytest.mark.parametrize(
    "bad",
    [
        {"lines": []},
        {"lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 101}]},
        {"lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 0, "line_total": 5}]},
        {"lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 0, "user_id": "u"}]},
        {"currency": "inr"},
        {"total_amount": 100},
    ],
)
def test_malformed_order_arguments_rejected(bad, no_database):
    result = execute_tool(make_agent_user(Role.PROCUREMENT_MANAGER), ORDER, {**VALID[ORDER], **bad}, thread_id="t")
    assert result.is_error and parse(result)["error"].startswith(f"Invalid arguments for {ORDER}")


@pytest.mark.parametrize(
    "bad",
    [
        {"lines": []},
        {"location": ""},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 0}]},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": -3}]},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": "6"}]},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 6, "line_position": -1}]},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 6, "unit_price": 1}]},
        {"lines": [{"purchase_order": "PO-2026-0057", "item": "BO-MC-0172", "quantity": 6, "po_line_id": "x"}]},
        {"lines": [{"item": "BO-MC-0172", "quantity": 6}]},
        {"currency": "INR"},
        {"unit_cost": 18.06},
    ],
)
def test_malformed_receipt_arguments_rejected(bad, no_database):
    result = execute_tool(make_agent_user(Role.PROCUREMENT_MANAGER), RECEIPT, {**VALID[RECEIPT], **bad},
                          thread_id="t")
    assert result.is_error and parse(result)["error"].startswith(f"Invalid arguments for {RECEIPT}")


def test_mutation_outside_a_conversation_is_refused(no_database):
    result = execute_tool(make_agent_user(Role.INVENTORY_MANAGER), MOVE, VALID[MOVE])
    assert parse(result) == {"error": "Changes can only be proposed within a conversation"}


def test_authorized_mutation_is_only_proposed(monkeypatch):
    proposed = []

    def fake_propose(user, thread_id, name, arguments):
        proposed.append((user, thread_id, name, arguments))
        return MutationStatus(proposal_id="p1", operation=name, state=MutationState.CONFIRMATION_REQUIRED,
                              message="Please confirm")

    monkeypatch.setattr(mutations, "propose", fake_propose)
    user = make_agent_user(Role.INVENTORY_MANAGER)
    result = execute_tool(user, MOVE, VALID[MOVE], thread_id="thread-1")
    assert not result.is_error and result.mutation.state is MutationState.CONFIRMATION_REQUIRED
    assert parse(result)["result"]["state"] == "confirmation_required"
    assert proposed == [(user, "thread-1", MOVE, {**VALID[MOVE], "quantity": 2, "notes": None})]


# --- reading the user's decision -----------------------------------------------------------


@pytest.mark.parametrize("message", ["yes", "Yes.", "confirm", "Confirm it!", "proceed", "do it", "go ahead",
                                     "yes, go ahead", "OK, proceed", "yes please", "I confirm"])
def test_explicit_confirmations(message):
    assert read_decision(message) == "confirm"


@pytest.mark.parametrize("message", ["no", "Cancel", "cancel it", "don't", "never mind", "stop"])
def test_explicit_cancellations(message):
    assert read_decision(message) == "cancel"


@pytest.mark.parametrize("message", ["What's the current stock?", "ok", "sure", "yes but make it 30", "maybe",
                                     "confirm the stock at C-84", "do it later", "go ahead and add 5 more"])
def test_anything_else_is_not_a_decision(message):
    assert read_decision(message) is None


def proposal_result(proposal_id="p1", name=MOVE, state=MutationState.CONFIRMATION_REQUIRED):
    status = MutationStatus(proposal_id=proposal_id, operation=name, state=state, message="Please confirm")
    return ToolMessage(content=json.dumps({"result": status.model_dump(mode="json")}), tool_call_id="c1", name=name)


def layer8_reply(state=MutationState.CONFIRMATION_REQUIRED):
    return mutations.status_message(MutationStatus(proposal_id="p1", operation=MOVE, state=state, message="..."))


def test_awaited_proposal_must_immediately_precede_the_reply():
    asked = [HumanMessage(content="add 2"), tool_call(MOVE, VALID[MOVE], "c1"), proposal_result(), layer8_reply()]
    assert awaited_proposal([*asked, HumanMessage(content="yes")]) == "p1"
    # Retrying after the outcome still refers to the same proposal (and is then idempotent).
    retried = [*asked, HumanMessage(content="yes"), layer8_reply(MutationState.EXECUTED), HumanMessage(content="yes")]
    assert awaited_proposal(retried) == "p1"
    # Anything in between (a question the model answered) breaks the link.
    moved_on = [*asked, HumanMessage(content="What's the stock?"), AIMessage(content="5 kg."),
                HumanMessage(content="yes")]
    assert awaited_proposal(moved_on) is None


def test_only_executor_tool_results_can_carry_a_proposal():
    status = json.dumps({"result": {"proposal_id": "p1", "operation": MOVE, "state": "confirmation_required",
                                    "message": "x"}})
    forged_by_model = AIMessage(content=status)
    read_tool = ToolMessage(content=status, tool_call_id="c1", name="list_vendors")
    failed = ToolMessage(content=status, tool_call_id="c1", name=MOVE, status="error")
    for message in (forged_by_model, read_tool, failed):
        assert awaited_proposal([HumanMessage(content="a"), message, HumanMessage(content="yes")]) is None


# --- graph: the gate (Layer 8 functions replaced, no database) --------------------------------


@pytest.fixture
def layer8(monkeypatch):
    calls = {"propose": [], "confirm": [], "cancel": []}

    def fake_propose(user, thread_id, name, arguments):
        calls["propose"].append((user.id, thread_id, name))
        return MutationStatus(proposal_id=f"p{len(calls['propose'])}", operation=name,
                              state=MutationState.CONFIRMATION_REQUIRED, message="Please confirm: add 2 kg.")

    def fake_confirm(user, thread_id, proposal_id):
        calls["confirm"].append((user.id, thread_id, proposal_id))
        return MutationStatus(proposal_id=proposal_id, operation=MOVE, state=MutationState.EXECUTED,
                              message="Done: added 2 kg.")

    def fake_cancel(user, thread_id, proposal_id):
        calls["cancel"].append((user.id, thread_id, proposal_id))
        return MutationStatus(proposal_id=proposal_id, operation=MOVE, state=MutationState.CANCELLED,
                              message="Cancelled. Nothing was changed.")

    monkeypatch.setattr(mutations, "propose", fake_propose)
    monkeypatch.setattr(mutations, "confirm_proposal", fake_confirm)
    monkeypatch.setattr(mutations, "cancel_proposal", fake_cancel)
    return calls


def make_agent(responses):
    model = ScriptedChatModel(responses=responses)
    return Agent(model, InMemorySaver(), 12), model


def claiming_call():
    """A model turn that calls the tool while already claiming success."""
    call = tool_call(MOVE, VALID[MOVE], "c1")
    return AIMessage(content="Done, stock updated!", tool_calls=call.tool_calls)


def test_proposal_ends_the_turn_with_layer8_wording(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, model = make_agent([claiming_call()])
    conversation = uuid.uuid4()
    reply = agent.respond(user, conversation, "Add 2 kg of BO-MC-0100 at 132-1")
    assert reply.text == "Please confirm: add 2 kg." and "Done" not in reply.text
    assert reply.mutation.state is MutationState.CONFIRMATION_REQUIRED and len(model.calls) == 1
    assert layer8["propose"] == [(user.id, thread_id_for(user, conversation), MOVE)]
    assert layer8["confirm"] == []


def test_explicit_confirmation_executes_without_the_model(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, model = make_agent([tool_call(MOVE, VALID[MOVE])])
    conversation = uuid.uuid4()
    agent.chat(user, conversation, "Add 2 kg of BO-MC-0100 at 132-1")
    reply = agent.respond(user, conversation, "confirm")
    assert reply.text == "Done: added 2 kg." and reply.mutation.state is MutationState.EXECUTED
    assert layer8["confirm"] == [(user.id, thread_id_for(user, conversation), "p1")]
    assert len(model.calls) == 1  # the confirmation turn made no model call


def test_cancellation(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, _ = make_agent([tool_call(MOVE, VALID[MOVE])])
    conversation = uuid.uuid4()
    agent.chat(user, conversation, "Add 2 kg")
    assert agent.chat(user, conversation, "cancel") == "Cancelled. Nothing was changed."
    assert layer8["cancel"] == [(user.id, thread_id_for(user, conversation), "p1")] and layer8["confirm"] == []


def test_unrelated_message_does_not_confirm_and_breaks_the_link(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, model = make_agent([tool_call(MOVE, VALID[MOVE]), AIMessage(content="It is 5 kg."),
                               AIMessage(content="Yes to what?")])
    conversation = uuid.uuid4()
    agent.chat(user, conversation, "Add 2 kg")
    assert agent.chat(user, conversation, "What's the current stock?") == "It is 5 kg."
    assert agent.chat(user, conversation, "yes") == "Yes to what?"
    assert layer8["confirm"] == [] and len(model.calls) == 3


def test_model_cannot_confirm_by_calling_the_tool_again(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, _ = make_agent([tool_call(MOVE, VALID[MOVE]), tool_call(MOVE, VALID[MOVE])])
    conversation = uuid.uuid4()
    agent.chat(user, conversation, "Add 2 kg")
    reply = agent.respond(user, conversation, "please go ahead and do it now")  # not an exact confirmation
    assert reply.mutation.state is MutationState.CONFIRMATION_REQUIRED
    assert layer8["confirm"] == [] and len(layer8["propose"]) == 2


def test_another_user_cannot_confirm(layer8):
    alice, mallory = make_agent_user(Role.INVENTORY_MANAGER), make_agent_user(Role.INVENTORY_MANAGER)
    agent, _ = make_agent([tool_call(MOVE, VALID[MOVE]), AIMessage(content="Nothing to confirm.")])
    conversation = uuid.uuid4()
    agent.chat(alice, conversation, "Add 2 kg")
    # Same conversation id, different user: a different, empty thread with nothing awaited.
    assert agent.chat(mallory, conversation, "confirm") == "Nothing to confirm."
    assert layer8["confirm"] == []


def test_one_change_per_turn(layer8):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    both = AIMessage(content="", tool_calls=[*tool_call(MOVE, VALID[MOVE], "a").tool_calls,
                                            *tool_call(MOVE, {**VALID[MOVE], "quantity": 3}, "b").tool_calls])
    agent, _ = make_agent([both])
    conversation = uuid.uuid4()
    agent.chat(user, conversation, "Add 2 and 3 kg")
    assert len(layer8["propose"]) == 1
    state = agent.graph.get_state({"configurable": {"thread_id": thread_id_for(user, conversation)}})
    refused = [m for m in state.values["messages"] if isinstance(m, ToolMessage) and m.status == "error"]
    assert [json.loads(m.content)["error"] for m in refused] == [
        "Only one change can be proposed at a time; this one was not proposed"
    ]


# --- live database: the full lifecycle (rolled back) -------------------------------------------


@pytest.fixture
def db(conn, monkeypatch):
    """Layer 8 on the test's rolled-back connection; its transactions become savepoints."""

    class Shared:
        @contextmanager
        def connect(self):
            yield conn

    monkeypatch.setattr(mutations, "get_engine", lambda: Shared())
    return conn


@pytest.fixture
def stocked(conn):
    return conn.execute(
        text(
            """
            SELECT s.item_id, i.item_code, l.name AS location, s.quantity
            FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id JOIN inv_locations l ON l.id = s.location_id
            WHERE i.status = 'active' AND l.is_active AND s.quantity >= 10
            ORDER BY s.item_id LIMIT 1
            """
        )
    ).mappings().one()


@pytest.fixture
def inv_user(demo_users):
    return demo_users["inventory_manager"]


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


def stock(conn, stocked):
    return Decimal(str(scalar(
        conn,
        "SELECT s.quantity FROM inv_current_stock s JOIN inv_locations l ON l.id = s.location_id"
        " WHERE s.item_id = :i AND l.name = :l",
        i=stocked["item_id"], l=stocked["location"],
    )))


def business_counts(conn):
    tables = ("inv_transactions", "proc_purchase_orders", "proc_po_lines")
    counts = {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in tables}
    counts["layer4_audit"] = scalar(conn, "SELECT count(*) FROM agent_audit_log WHERE operation NOT LIKE :p",
                                    p="%:request")
    return counts


def request_row(conn, proposal_id):
    return conn.execute(
        text("SELECT * FROM agent_audit_log WHERE operation_id = :id"), {"id": proposal_id}
    ).mappings().one()


def move(stocked, direction="inbound", quantity=2):
    return args(MOVE, item=stocked["item_code"], location=stocked["location"], direction=direction, quantity=quantity)


def test_proposal_changes_nothing_and_is_audited_as_pending(db, inv_user, stocked):
    before, quantity = business_counts(db), stock(db, stocked)
    thread = thread_id_for(inv_user, uuid.uuid4())
    status = mutations.propose(inv_user, thread, MOVE, move(stocked))
    assert status.state is MutationState.CONFIRMATION_REQUIRED
    assert status.message.startswith("Please confirm: add 2 ") and "Nothing has been changed yet" in status.message
    assert Decimal(status.details["quantity_after"]) == quantity + 2 and "transaction_id" not in status.details
    assert business_counts(db) == before and stock(db, stocked) == quantity

    row = request_row(db, status.proposal_id)
    assert (row["status"], row["operation"], row["entity_type"]) == ("pending", f"{MOVE}:request", "inventory_transaction")
    assert (row["user_id"], row["role"], row["thread_id"]) == (inv_user.id, "inventory_manager", thread)
    assert row["before_state"]["arguments"] == move(stocked) and row["completed_at"] is None


def test_confirmation_executes_exactly_once(db, inv_user, stocked):
    quantity = stock(db, stocked)
    before = business_counts(db)
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))

    done = mutations.confirm_proposal(inv_user, thread, proposal.proposal_id)
    assert done.state is MutationState.EXECUTED and done.message.startswith("Done: added 2 ")
    assert stock(db, stocked) == quantity + 2
    after = business_counts(db)
    assert after["inv_transactions"] == before["inv_transactions"] + 1
    assert after["layer4_audit"] == before["layer4_audit"] + 1  # Layer 4's own success record, unchanged

    row = request_row(db, proposal.proposal_id)
    assert row["status"] == "success" and row["entity_id"] == done.entity_id and row["completed_at"] is not None
    layer4 = request_row(db, row["after_state"]["audit_operation_id"])
    assert (layer4["operation"], layer4["status"], layer4["entity_id"]) == (MOVE, "success", done.entity_id)

    for attempt in (1, 2):  # retries and double clicks
        again = mutations.confirm_proposal(inv_user, thread, proposal.proposal_id)
        assert again.state is MutationState.ALREADY_EXECUTED and again.entity_id == done.entity_id
        assert request_row(db, proposal.proposal_id)["after_state"]["duplicate_confirmations"] == attempt
    assert stock(db, stocked) == quantity + 2 and business_counts(db) == after


def test_failed_execution_rolls_back_and_is_audited(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    quantity = stock(db, stocked)
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked, "outbound", float(quantity)))
    # Before the user confirms, someone else removes stock, so the proposal can no longer be honoured.
    inventory.record_stock_movement(db, inv_user, item=stocked["item_code"], location=stocked["location"],
                                    direction="outbound", quantity=1)
    before = business_counts(db)

    failed = mutations.confirm_proposal(inv_user, thread, proposal.proposal_id)
    assert failed.state is MutationState.FAILED and failed.message.startswith("The change was not made: Cannot remove")
    assert business_counts(db) == before and stock(db, stocked) == quantity - 1  # nothing partial
    row = request_row(db, proposal.proposal_id)
    assert row["status"] == "failed" and row["after_state"] == {"outcome": "failed"}
    assert row["error"].startswith("InsufficientStockError: Cannot remove")
    # A failed proposal is final: confirming again does not retry it.
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.NOT_PENDING


def test_cancelled_proposal_cannot_be_confirmed(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    before = business_counts(db)
    assert mutations.cancel_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.CANCELLED
    row = request_row(db, proposal.proposal_id)
    assert (row["status"], row["after_state"], row["error"]) == ("failed", {"outcome": "cancelled"},
                                                                 "cancelled by the user")
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.NOT_PENDING
    assert business_counts(db) == before


def test_expired_proposal_is_not_executed(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    db.execute(text("UPDATE agent_audit_log SET created_at = now() - interval '1 hour' WHERE operation_id = :id"),
               {"id": proposal.proposal_id})
    before = business_counts(db)
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.EXPIRED
    assert request_row(db, proposal.proposal_id)["after_state"] == {"outcome": "expired"}
    # Expiry is final: confirming again does not revive it.
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.NOT_PENDING
    assert business_counts(db) == before


def test_proposal_is_confirmable_until_its_ttl(db, inv_user, stocked):
    # now() is the transaction's start time throughout the rolled-back test, so the age is exact.
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    db.execute(text("UPDATE agent_audit_log SET created_at = now() - make_interval(secs => :age) "
                    "WHERE operation_id = :id"), {"age": PROPOSAL_TTL_SECONDS - 60, "id": proposal.proposal_id})
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.EXECUTED


def test_proposal_belongs_to_its_user_and_thread(db, demo_users, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    before = business_counts(db)
    other_user = demo_users["procurement_manager"]
    attempts = [
        (other_user, thread),  # another user, even with the right thread id
        (inv_user, thread_id_for(inv_user, uuid.uuid4())),  # the right user, another conversation
    ]
    for user, other_thread in attempts:
        assert mutations.confirm_proposal(user, other_thread, proposal.proposal_id).state is MutationState.NOT_PENDING
        assert mutations.cancel_proposal(user, other_thread, proposal.proposal_id).state is MutationState.NOT_PENDING
    assert request_row(db, proposal.proposal_id)["status"] == "pending" and business_counts(db) == before
    assert mutations.confirm_proposal(inv_user, thread, proposal.proposal_id).state is MutationState.EXECUTED


def test_new_proposal_supersedes_and_identical_one_is_reused(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    first = mutations.propose(inv_user, thread, MOVE, move(stocked))
    assert mutations.propose(inv_user, thread, MOVE, move(stocked)).proposal_id == first.proposal_id
    second = mutations.propose(inv_user, thread, MOVE, move(stocked, quantity=3))
    assert second.proposal_id != first.proposal_id
    assert request_row(db, first.proposal_id)["after_state"] == {"outcome": "superseded"}
    assert mutations.confirm_proposal(inv_user, thread, first.proposal_id).state is MutationState.NOT_PENDING
    pending = scalar(db, "SELECT count(*) FROM agent_audit_log WHERE thread_id = :t AND status = 'pending'", t=thread)
    assert pending == 1


def test_layer4_validation_runs_at_proposal(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    with pytest.raises(NotFoundError):
        mutations.propose(inv_user, thread, MOVE, move({**stocked, "item_code": "NO-SUCH-ITEM"}))
    result = execute_tool(inv_user, MOVE, {**move(stocked), "location": "No Such Place"}, thread_id=thread)
    assert parse(result) == {"error": "Location 'No Such Place' not found"}
    assert scalar(db, "SELECT count(*) FROM agent_audit_log WHERE thread_id = :t", t=thread) == 0


def test_permission_is_rechecked_at_confirmation(db, inv_user, stocked):
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    before = business_counts(db)
    # The same person, whose role changed (e.g. to owner) before confirming.
    demoted = AuthenticatedUser(**{**inv_user.model_dump(), "role": Role.OWNER})
    denied = mutations.confirm_proposal(demoted, thread, proposal.proposal_id)
    assert denied.state is MutationState.FAILED and "permission" in denied.message
    assert request_row(db, proposal.proposal_id)["after_state"] == {"outcome": "denied"}
    assert business_counts(db) == before


def test_database_failure_is_safe_and_recorded(db, inv_user, stocked, monkeypatch):
    thread = thread_id_for(inv_user, uuid.uuid4())
    proposal = mutations.propose(inv_user, thread, MOVE, move(stocked))
    url = get_settings().database_url

    def broken(conn, user, **kwargs):
        raise OperationalError("SELECT secret FROM users", {}, Exception(f"could not connect to {url}"))

    monkeypatch.setitem(MUTATIONS, MOVE, dataclasses.replace(MUTATIONS[MOVE], operation=broken))
    failed = mutations.confirm_proposal(inv_user, thread, proposal.proposal_id)
    assert failed.state is MutationState.FAILED
    assert failed.message == "The change could not be completed; nothing was saved. Please try again later."
    for leak in ("SELECT", "secret", url, "postgresql"):
        assert leak not in failed.model_dump_json()
    row = request_row(db, proposal.proposal_id)
    assert row["status"] == "failed" and url not in (row["error"] or "")


def test_purchase_order_preview_matches_layer4_and_is_created_once(db, demo_users):
    pm = demo_users["procurement_manager"]
    vendor = scalar(db, "SELECT id FROM proc_vendors WHERE is_active ORDER BY name LIMIT 1")
    items = db.execute(text("SELECT item_code FROM inv_items WHERE status = 'active' ORDER BY item_code LIMIT 2")
                       ).scalars().all()
    lines = [{"item": items[0], "quantity": 1.5, "unit_price": 0.67, "tax_percentage": 18},
             {"item": items[1], "quantity": 2, "unit_price": 100, "tax_percentage": 5}]
    thread = thread_id_for(pm, uuid.uuid4())
    before = business_counts(db)

    proposal = mutations.propose(pm, thread, ORDER, args(ORDER, vendor_id=vendor, lines=lines))
    expected = [calculate_line_amounts(Decimal(str(line["quantity"])), Decimal(str(line["unit_price"])),
                                       Decimal(str(line["tax_percentage"]))) for line in lines]
    total = sum(e["line_total"] for e in expected)
    assert Decimal(proposal.details["total_amount"]) == total
    assert [Decimal(line["line_total"]) for line in proposal.details["lines"]] == [e["line_total"] for e in expected]
    assert "po_number" not in proposal.details and "PO number is assigned when it is created" in proposal.message
    assert business_counts(db) == before

    created = mutations.confirm_proposal(pm, thread, proposal.proposal_id)
    assert created.state is MutationState.EXECUTED and created.details["po_number"] in created.message
    assert Decimal(created.details["total_amount"]) == total
    after = business_counts(db)
    assert (after["proc_purchase_orders"], after["proc_po_lines"]) == (
        before["proc_purchase_orders"] + 1, before["proc_po_lines"] + 2
    )
    for _ in range(2):
        assert mutations.confirm_proposal(pm, thread, proposal.proposal_id).state is MutationState.ALREADY_EXECUTED
    assert business_counts(db) == after  # no duplicate PO
    assert request_row(db, proposal.proposal_id)["entity_id"] == created.entity_id


def test_conversation_end_to_end(db, inv_user, stocked, monkeypatch):
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())  # no read tool is needed
    quantity = stock(db, stocked)
    agent, model = make_agent([tool_call(MOVE, move(stocked, quantity=1))])
    conversation = uuid.uuid4()

    proposed = agent.respond(inv_user, conversation, f"Add 1 of {stocked['item_code']} at {stocked['location']}")
    assert proposed.mutation.state is MutationState.CONFIRMATION_REQUIRED and stock(db, stocked) == quantity
    done = agent.respond(inv_user, conversation, "yes")
    assert done.mutation.state is MutationState.EXECUTED and done.text.startswith("Done: added 1 ")
    assert stock(db, stocked) == quantity + 1
    repeated = agent.respond(inv_user, conversation, "yes")
    assert repeated.mutation.state is MutationState.ALREADY_EXECUTED and stock(db, stocked) == quantity + 1
    assert len(model.calls) == 1


# --- live database: committed, for concurrency and connection handling ------------------------


FAKE_MOVEMENT = {
    "transaction": {"id": "fake-transaction"},
    "item": {"item_code": "FAKE-0001", "name": "Fake", "unit": "nos"},
    "location": {"name": "Nowhere"},
    "direction": "inbound",
    "quantity_before": 1,
    "quantity_after": 2,
    "audit_operation_id": "fake-audit",
}


@pytest.fixture
def committed(live_db, demo_users, monkeypatch):
    """A real engine with an operation that writes no business data; deletes its audit rows afterwards."""
    calls = []

    def slow_operation(conn, user, **kwargs):
        calls.append(kwargs)
        time.sleep(0.5)  # widen the window for a competing confirmation
        return FAKE_MOVEMENT

    monkeypatch.setitem(MUTATIONS, MOVE, dataclasses.replace(MUTATIONS[MOVE], operation=slow_operation))
    user = demo_users["inventory_manager"]
    thread = thread_id_for(user, uuid.uuid4())
    yield user, thread, calls
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM agent_audit_log WHERE thread_id = :t"), {"t": thread})


def test_no_connection_or_transaction_is_held_while_awaiting_confirmation(committed):
    user, thread, _ = committed
    proposal = mutations.propose(user, thread, MOVE, {"item": "FAKE-0001"})
    assert get_engine().pool.checkedout() == 0
    with get_engine().connect() as other:  # committed: visible to any later request
        assert scalar(other, "SELECT status FROM agent_audit_log WHERE operation_id = :id",
                      id=proposal.proposal_id) == "pending"


def committed_request(user, thread):
    proposal_id = str(uuid.uuid4())
    with get_engine().begin() as conn:
        audit.insert_request(conn, operation_id=proposal_id, user_id=user.id, role=user.role.value,
                             operation=f"{MOVE}:request", entity_type="inventory_transaction",
                             before_state={"arguments": {"item": "FAKE-0001"}}, thread_id=thread)
    return proposal_id


def test_concurrent_confirmations_execute_once(committed):
    user, thread, calls = committed
    proposal_id = committed_request(user, thread)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: mutations.confirm_proposal(user, thread, proposal_id), range(2)))

    assert sorted(r.state for r in results) == [MutationState.ALREADY_EXECUTED, MutationState.EXECUTED]
    assert len(calls) == 1
    with get_engine().connect() as conn:
        row = request_row(conn, proposal_id)
    assert row["status"] == "success" and row["after_state"]["duplicate_confirmations"] == 1
    assert get_engine().pool.checkedout() == 0


def test_concurrent_confirmation_and_cancellation_resolve_once(committed):
    # Whichever locks the request first wins; the other sees it is no longer pending.
    user, thread, calls = committed
    proposal_id = committed_request(user, thread)

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmed = pool.submit(mutations.confirm_proposal, user, thread, proposal_id)
        cancelled = pool.submit(mutations.cancel_proposal, user, thread, proposal_id)
        states = {confirmed.result().state, cancelled.result().state}

    with get_engine().connect() as conn:
        row = request_row(conn, proposal_id)
    if MutationState.EXECUTED in states:
        assert states == {MutationState.EXECUTED, MutationState.NOT_PENDING} and len(calls) == 1
        assert row["status"] == "success"
    else:
        assert states == {MutationState.CANCELLED, MutationState.NOT_PENDING} and calls == []
        assert (row["status"], row["after_state"]) == ("failed", {"outcome": "cancelled"})
    assert get_engine().pool.checkedout() == 0
