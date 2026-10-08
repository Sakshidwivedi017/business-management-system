"""Mutation safety (Layer 8): proposal, explicit confirmation, idempotent execution and audit.

A mutation tool call never changes data. It only proposes:

1. The Layer 4 operation runs in a transaction that is always rolled back, so Layer 4's
   own permission check, validation and calculations (stock after the move, PO totals)
   produce the preview. Nothing is validated twice.
2. The proposal is stored as a pending agent_audit_log row (operation "<name>:request")
   owned by the authenticated user and the conversation thread, and the user is asked
   to confirm in a message built from the preview, never written by the model.

The change happens only when the user's own next message is an explicit confirmation
(or Layer 9 calls confirm_proposal for that user and thread). Execution row-locks the
pending request, re-runs the Layer 4 operation with the stored arguments and marks the
request in one short transaction, so a proposal executes at most once however often or
concurrently it is confirmed. A failed operation rolls back to a savepoint, and its
failure is recorded in the same transaction. No transaction is held across turns, model
calls or network I/O.

The request's operation_id is the idempotency key. Statuses are the existing ones:
pending (awaiting confirmation), success (executed) and failed; after_state.outcome
says which kind of failure (failed, denied, cancelled, superseded, expired), and a
repeated confirmation of an executed request is counted in after_state.
"""

import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.auth.permissions import AuthenticatedUser, PermissionDenied
from app.db.connection import atomic, get_engine
from app.db.repositories import audit
from app.services import BusinessError, inventory, procurement

logger = logging.getLogger(__name__)

# A proposal not confirmed within this time can no longer be executed.
PROPOSAL_TTL_SECONDS = 30 * 60
REQUEST_SUFFIX = ":request"
# Marks the assistant messages Layer 8 writes, in response_metadata (never sent to the model provider).
SOURCE = "mutation_safety"
MAX_ERROR_LENGTH = 500

_CONFIRM_HINT = '\n\nReply "confirm" to go ahead or "cancel" to discard it. Nothing has been changed yet.'
_UNAVAILABLE = "The change could not be completed; nothing was saved. Please try again later."


class MutationState(StrEnum):
    CONFIRMATION_REQUIRED = "confirmation_required"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ALREADY_EXECUTED = "already_executed"
    NOT_PENDING = "not_pending"


class MutationStatus(BaseModel):
    """What happened to a proposed change. `message` is built from Layer 4 or stored data only."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str | None
    operation: str | None
    state: MutationState
    message: str
    entity_id: str | None = None
    details: dict[str, Any] = {}


# --- the exposed mutations ---------------------------------------------------------


def _num(value: Any) -> str:
    """Exact decimal text without trailing zeros: 20.000 -> "20", 102.50 -> "102.5"."""
    number = Decimal(str(value)).normalize()
    return format(number, "f")


def _movement_facts(result: dict[str, Any]) -> dict[str, Any]:
    before, after = Decimal(str(result["quantity_before"])), Decimal(str(result["quantity_after"]))
    return {
        "transaction_id": result["transaction"]["id"],
        "item_code": result["item"]["item_code"],
        "item_name": result["item"]["name"],
        "unit": result["item"]["unit"],
        "location": result["location"]["name"],
        "direction": result["direction"],
        "quantity": _num(abs(after - before)),
        "quantity_before": _num(before),
        "quantity_after": _num(after),
    }


def _movement_text(facts: dict[str, Any], done: bool) -> str:
    inbound = facts["direction"] == "inbound"
    what = f"{facts['quantity']} {facts['unit']} of {facts['item_code']} ({facts['item_name']})"
    where = f"{'at' if inbound else 'from'} {facts['location']}"
    if done:
        return (
            f"Done: {'added' if inbound else 'removed'} {what} {where}. Stock there is now "
            f"{facts['quantity_after']} {facts['unit']} (was {facts['quantity_before']})."
        )
    return (
        f"Please confirm: {'add' if inbound else 'remove'} {what} {where}. Stock there would go from "
        f"{facts['quantity_before']} to {facts['quantity_after']} {facts['unit']}."
    )


def _order_facts(result: dict[str, Any]) -> dict[str, Any]:
    header = result["purchase_order"]
    return {
        "po_id": header["id"],
        "po_number": header["po_number"],
        "vendor_id": header["vendor_id"],
        "vendor_name": header["vendor_name"],
        "currency": header["currency"],
        "subtotal": _num(header["subtotal"]),
        "tax_amount": _num(header["tax_amount"]),
        "total_amount": _num(header["total_amount"]),
        "lines": [
            {
                "item_code": line["item_code"],
                "item_name": line["item_name"],
                "quantity": _num(line["quantity_ordered"]),
                "unit": line["unit"],
                "unit_price": _num(line["unit_price"]),
                "tax_percentage": _num(line["tax_percentage"]),
                "line_total": _num(line["line_total"]),
            }
            for line in result["lines"]
        ],
    }


def _order_text(facts: dict[str, Any], done: bool) -> str:
    total = f"{facts['currency']} {facts['total_amount']}"
    if done:
        return f"Done: purchase order {facts['po_number']} was created for {facts['vendor_name']}, total {total}."
    lines = "\n".join(
        f"- {line['item_code']} ({line['item_name']}): {line['quantity']} {line['unit']} x {line['unit_price']} "
        f"+ {line['tax_percentage']}% tax = {line['line_total']}"
        for line in facts["lines"]
    )
    return (
        f"Please confirm: create a purchase order to {facts['vendor_name']} with {len(facts['lines'])} "
        f"line(s):\n{lines}\nTotal {total} (subtotal {facts['subtotal']}, tax {facts['tax_amount']}). "
        "The PO number is assigned when it is created."
    )


def _receipt_facts(result: dict[str, Any]) -> dict[str, Any]:
    # Quantities and statuses only: the purchase price is not needed to confirm a receipt.
    return {
        "receipt_id": result["receipts"][0]["receipt_id"],
        "location": result["location"]["name"],
        "lines": [
            {
                "po_number": receipt["po_number"],
                "line_position": receipt["line_position"],
                "item_code": receipt["item_code"],
                "item_name": receipt["item_name"],
                "unit": receipt["unit"] or "",
                "quantity": _num(receipt["quantity"]),
                "quantity_ordered": _num(receipt["quantity_ordered"]),
                "received_before": _num(receipt["received_before"]),
                "received_after": _num(receipt["received_after"]),
            }
            for receipt in result["receipts"]
        ],
        "purchase_orders": [
            {key: order[key] for key in ("po_number", "status_before", "status_after")}
            for order in result["purchase_orders"]
        ],
    }


def _receipt_text(facts: dict[str, Any], done: bool) -> str:
    where = facts["location"]
    lines = "\n".join(
        f"- {line['po_number']}: {line['quantity']} {line['unit']} of {line['item_code']} ({line['item_name']}); "
        f"received {line['received_before']} -> {line['received_after']} of {line['quantity_ordered']} {line['unit']}"
        for line in facts["lines"]
    )
    statuses = "; ".join(
        f"{order['po_number']} {'is now' if done else 'would become'} {order['status_after']}"
        if order["status_after"] != order["status_before"]
        else f"{order['po_number']} {'stays' if done else 'would stay'} {order['status_after']}"
        for order in facts["purchase_orders"]
    )
    if done:
        return f"Done: recorded goods received at {where}:\n{lines}\nStock at {where} was increased; {statuses}."
    return (
        f"Please confirm: record goods received at {where} against {len(facts['lines'])} purchase order line(s):\n"
        f"{lines}\nThis adds the quantities to stock at {where} and updates the purchase orders: {statuses}."
    )


@dataclass(frozen=True)
class Mutation:
    operation: Callable[..., dict[str, Any]]  # the Layer 4 operation: the only code that changes data
    entity_type: str
    facts: Callable[[dict[str, Any]], dict[str, Any]]  # compact, JSON-safe facts from its result
    text: Callable[[dict[str, Any], bool], str]
    entity_key: str  # fact holding the created entity's id
    assigned_on_execution: tuple[str, ...]  # facts that a rolled-back preview cannot know


MUTATIONS: dict[str, Mutation] = {
    "record_stock_movement": Mutation(
        inventory.record_stock_movement, "inventory_transaction", _movement_facts, _movement_text,
        "transaction_id", ("transaction_id",),
    ),
    "create_purchase_order": Mutation(
        procurement.create_purchase_order, "purchase_order", _order_facts, _order_text,
        "po_id", ("po_id", "po_number"),
    ),
    "record_purchase_receipt": Mutation(
        procurement.record_purchase_receipt, "purchase_receipt", _receipt_facts, _receipt_text,
        "receipt_id", ("receipt_id",),
    ),
}


# --- proposal -------------------------------------------------------------------------


def propose(user: AuthenticatedUser, thread_id: str, name: str, arguments: dict[str, Any]) -> MutationStatus:
    """Validate and preview a change through Layer 4 without keeping it, and store it as pending.

    Raises what Layer 4 raises (PermissionDenied, BusinessError); nothing is stored then.
    """
    mutation = MUTATIONS[name]
    with get_engine().connect() as conn:
        preview = _preview(conn, user, mutation, arguments)
        message = mutation.text(preview, False) + _CONFIRM_HINT
        with atomic(conn):
            audit.lock_thread(conn, thread_id)
            proposal_id = audit.find_pending_request(
                conn, user_id=user.id, thread_id=thread_id, operation=name + REQUEST_SUFFIX, arguments=arguments,
                ttl_seconds=PROPOSAL_TTL_SECONDS,
            )
            if proposal_id is None:
                # One pending change per conversation: a new proposal replaces an unconfirmed one.
                audit.close_pending_requests(
                    conn, user_id=user.id, thread_id=thread_id, operation_suffix=REQUEST_SUFFIX,
                    after_state={"outcome": "superseded"}, error="superseded by a newer proposal",
                )
                proposal_id = str(uuid.uuid4())
                audit.insert_request(
                    conn,
                    operation_id=proposal_id,
                    user_id=user.id,
                    role=user.role.value,
                    operation=name + REQUEST_SUFFIX,
                    entity_type=mutation.entity_type,
                    before_state={"arguments": arguments, "preview": preview, "summary": message},
                    thread_id=thread_id,
                )
    logger.info("Mutation %s proposed by user %s", name, user.id)
    return MutationStatus(proposal_id=proposal_id, operation=name, state=MutationState.CONFIRMATION_REQUIRED,
                          message=message, details=preview)


def _preview(conn: Connection, user: AuthenticatedUser, mutation: Mutation, arguments: dict[str, Any]) -> dict:
    """Run the Layer 4 operation and always roll it back: a preview with Layer 4's own validation."""
    trial = conn.begin_nested() if conn.in_transaction() else conn.begin()
    try:
        facts = mutation.facts(mutation.operation(conn, user, **arguments))
    finally:
        trial.rollback()
    return {key: value for key, value in facts.items() if key not in mutation.assigned_on_execution}


# --- confirmation and execution ----------------------------------------------------------


def confirm_proposal(user: AuthenticatedUser, thread_id: str, proposal_id: str) -> MutationStatus:
    """Execute a pending proposal of this user's conversation at most once. Safe to repeat or race."""
    try:
        with get_engine().connect() as conn, atomic(conn):
            return _confirm_locked(conn, user, thread_id, proposal_id)
    except SQLAlchemyError as exc:
        # The transaction is gone, so the change was not made. Record that separately, best effort.
        logger.error("Confirming proposal %s failed: %s", proposal_id, type(exc).__name__)
        _record_failure_separately(proposal_id, user, thread_id)
        return MutationStatus(proposal_id=proposal_id, operation=None, state=MutationState.FAILED,
                              message=_UNAVAILABLE)


def _confirm_locked(conn: Connection, user: AuthenticatedUser, thread_id: str, proposal_id: str) -> MutationStatus:
    row = audit.lock_request(conn, operation_id=proposal_id, user_id=user.id, thread_id=thread_id,
                             ttl_seconds=PROPOSAL_TTL_SECONDS)
    name = row["operation"].removesuffix(REQUEST_SUFFIX) if row else None
    if row is None or name not in MUTATIONS or not row["operation"].endswith(REQUEST_SUFFIX):
        return _status(proposal_id, None, MutationState.NOT_PENDING, "There is no pending change to confirm.")
    mutation = MUTATIONS[name]

    if row["status"] == "success":
        audit.note_duplicate(conn, proposal_id)
        logger.info("Duplicate confirmation of proposal %s ignored", proposal_id)
        result = row["after_state"].get("result", {})
        return _status(proposal_id, name, MutationState.ALREADY_EXECUTED,
                       f"This change was already made, so nothing more was done. {row['after_state']['summary']}",
                       entity_id=result.get(mutation.entity_key), details=result)
    if row["status"] != "pending":
        outcome = (row["after_state"] or {}).get("outcome", "failed")
        return _status(proposal_id, name, MutationState.NOT_PENDING,
                       f"That change was not made ({outcome}), so there is nothing to confirm. "
                       "Ask again if you still want it.")
    if row["expired"]:
        audit.finish_request(conn, operation_id=proposal_id, status="failed", after_state={"outcome": "expired"},
                             error="expired before confirmation")
        return _status(proposal_id, name, MutationState.EXPIRED,
                       "That request expired before it was confirmed, so nothing was changed. "
                       "Ask again if you still want it.")

    try:
        with atomic(conn):  # savepoint: a failed operation leaves the request locked and recordable
            result = mutation.operation(conn, user, **row["before_state"]["arguments"])
    except (BusinessError, PermissionDenied) as exc:
        outcome = "denied" if isinstance(exc, PermissionDenied) else "failed"
        reason = "You do not have permission to make this change" if outcome == "denied" else str(exc)
        audit.finish_request(conn, operation_id=proposal_id, status="failed", after_state={"outcome": outcome},
                             error=f"{type(exc).__name__}: {reason}"[:MAX_ERROR_LENGTH])
        logger.info("Proposal %s failed on execution: %s", proposal_id, type(exc).__name__)
        return _status(proposal_id, name, MutationState.FAILED, f"The change was not made: {reason}")

    facts = mutation.facts(result)
    summary = mutation.text(facts, True)
    audit.finish_request(
        conn,
        operation_id=proposal_id,
        status="success",
        entity_id=facts[mutation.entity_key],
        after_state={"outcome": "executed", "audit_operation_id": result["audit_operation_id"], "result": facts,
                     "summary": summary},
    )
    logger.info("Proposal %s executed (%s)", proposal_id, name)
    return _status(proposal_id, name, MutationState.EXECUTED, summary, entity_id=facts[mutation.entity_key],
                   details=facts)


def cancel_proposal(user: AuthenticatedUser, thread_id: str, proposal_id: str) -> MutationStatus:
    with get_engine().connect() as conn, atomic(conn):
        row = audit.lock_request(conn, operation_id=proposal_id, user_id=user.id, thread_id=thread_id,
                                 ttl_seconds=PROPOSAL_TTL_SECONDS)
        name = row["operation"].removesuffix(REQUEST_SUFFIX) if row else None
        if row is None or row["status"] != "pending":
            message = ("That change was already made, so it cannot be cancelled here."
                       if row and row["status"] == "success" else "There is no pending change to cancel.")
            return _status(proposal_id, name, MutationState.NOT_PENDING, message)
        audit.finish_request(conn, operation_id=proposal_id, status="failed", after_state={"outcome": "cancelled"},
                             error="cancelled by the user")
    logger.info("Proposal %s cancelled", proposal_id)
    return _status(proposal_id, name, MutationState.CANCELLED, "Cancelled. Nothing was changed.")


def _record_failure_separately(proposal_id: str, user: AuthenticatedUser, thread_id: str) -> None:
    try:
        with get_engine().connect() as conn, atomic(conn):
            # Only this user's own pending request; no-op if it was not pending.
            if audit.lock_request(conn, operation_id=proposal_id, user_id=user.id, thread_id=thread_id,
                                  ttl_seconds=PROPOSAL_TTL_SECONDS):
                audit.finish_request(conn, operation_id=proposal_id, status="failed",
                                     after_state={"outcome": "failed"}, error="OperationFailedError: database error")
    except SQLAlchemyError as exc:
        logger.error("Could not record failure of proposal %s: %s", proposal_id, type(exc).__name__)


def _status(proposal_id: str, operation: str | None, state: MutationState, message: str, **extra) -> MutationStatus:
    return MutationStatus(proposal_id=proposal_id, operation=operation, state=state, message=message, **extra)


# --- conversation: which proposal a user message answers -----------------------------------

_CONFIRM = re.compile(
    r"(?:yes|yes please|confirm|confirmed|confirm it|i confirm|proceed|go ahead|do it|"
    r"(?:yes|ok|okay|please),? (?:confirm|confirm it|proceed|go ahead|do it))"
)
_CANCEL = re.compile(
    r"(?:no|nope|cancel|cancel it|don't|do not|stop|abort|never ?mind|no,? (?:cancel|cancel it|don't|stop))"
)


def read_decision(text: str) -> str | None:
    """"confirm" or "cancel" only when the whole message says exactly that; otherwise None."""
    normalized = " ".join(str(text).lower().replace("’", "'").split()).strip(" .!")
    if _CONFIRM.fullmatch(normalized):
        return "confirm"
    if _CANCEL.fullmatch(normalized):
        return "cancel"
    return None


def is_layer8_message(message: AnyMessage) -> bool:
    return isinstance(message, AIMessage) and message.response_metadata.get("source") == SOURCE


def status_message(status: MutationStatus) -> AIMessage:
    """The assistant's reply for a mutation step, written by Layer 8 rather than the model."""
    return AIMessage(content=status.message,
                     response_metadata={"source": SOURCE, "mutation": status.model_dump(mode="json")})


def proposal_in(message: AnyMessage) -> str | None:
    """The proposal id in a mutation tool result. Tool results are written by the executor, never by the model."""
    if not isinstance(message, ToolMessage) or message.name not in MUTATIONS or message.status == "error":
        return None
    try:
        result = MutationStatus.model_validate(json.loads(message.content)["result"])
    except (TypeError, ValueError, KeyError):
        return None
    return result.proposal_id if result.state is MutationState.CONFIRMATION_REQUIRED else None


def awaited_proposal(messages: Sequence[AnyMessage]) -> str | None:
    """The proposal the latest user message can answer: the last thing before it must be that proposal,
    optionally followed only by Layer 8 replies and earlier confirm/cancel messages (retries)."""
    humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not humans:
        return None
    for message in reversed(messages[: humans[-1]]):
        if is_layer8_message(message) or (isinstance(message, HumanMessage) and read_decision(message.text)):
            continue
        if isinstance(message, ToolMessage):
            proposal_id = proposal_in(message)
            if proposal_id:
                return proposal_id
            continue
        return None
    return None


def answer_pending(
    user: AuthenticatedUser, thread_id: str | None, messages: Sequence[AnyMessage]
) -> MutationStatus | None:
    """Act on an explicit confirm/cancel of the awaited proposal; None when the message is anything else."""
    if thread_id is None or not messages or not isinstance(messages[-1], HumanMessage):
        return None
    decision = read_decision(messages[-1].text)
    proposal_id = awaited_proposal(messages) if decision else None
    if proposal_id is None:
        return None
    if decision == "confirm":
        return confirm_proposal(user, thread_id, proposal_id)
    return cancel_proposal(user, thread_id, proposal_id)


def turn_mutation(messages: Sequence[AnyMessage]) -> MutationStatus | None:
    """The mutation step this turn ended with, for the application layer."""
    if messages and is_layer8_message(messages[-1]):
        return MutationStatus.model_validate(messages[-1].response_metadata["mutation"])
    return None
