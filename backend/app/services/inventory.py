"""Inventory business operations.

Every operation takes the authenticated user and checks permission before
touching the database. Writes are atomic (see app.db.connection.atomic) and
audited in the same transaction.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission, has_permission
from app.db.connection import atomic
from app.db.repositories import audit as audit_repo
from app.db.repositories import inventory as repo
from app.services import matching
from app.services.errors import InsufficientStockError, NotFoundError, ValidationError, database_errors
from app.services.validation import check_limit, optional_text, require_text, to_decimal

# Used wherever inv_current_stock.min_stock_level is NULL (currently every row):
# a stock row is "low" when its quantity is at or below this value.
LOW_STOCK_FALLBACK_THRESHOLD = Decimal("0")

MAX_MOVEMENT_QUANTITY = Decimal("1000000000")
MAX_NOTES_LENGTH = 500

# inv_transactions rows written by a purchase-order receipt carry procurement data: the PO they
# reference, the PO line's purchase price and the receipt's invoice notes. Readers without
# procurement:read get those rows with these fields set to None.
PURCHASE_ORDER_REFERENCE = "purchase_order"
PROCUREMENT_TRANSACTION_FIELDS = ("reference_type", "reference_id", "unit_cost", "currency", "notes")


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


def utcnow() -> datetime:
    # inv_* and proc_* timestamps are naive UTC (timestamp without time zone).
    return datetime.now(UTC).replace(tzinfo=None)


def resolve_item(conn: Connection, item: str) -> dict[str, Any]:
    """Find an item by id or item code (case and punctuation aside). Otherwise suggest, never guess."""
    ref = require_text(item, "item")
    found = repo.get_item_by_id(conn, ref) or repo.get_item_by_code(conn, ref)
    if found is None:
        codes = repo.list_item_codes(conn)
        code = matching.same_key(ref, (code for code, _ in codes))
        if code is None:
            raise NotFoundError(f"Item '{ref}' not found" + matching.suggestion(matching.close_matches(ref, codes)))
        found = repo.get_item_by_code(conn, code)
    return found


def resolve_location(conn: Connection, location: str) -> dict[str, Any]:
    """Find a location by id or its (unique) name, case and punctuation aside. Otherwise suggest, never guess."""
    ref = require_text(location, "location")
    found = repo.get_location_by_id(conn, ref) or repo.get_location_by_name(conn, ref)
    if found is None:
        locations = repo.list_locations(conn, active_only=False)
        name = matching.same_key(ref, (loc["name"] for loc in locations))
        if name is None:
            candidates = [(loc["name"], loc["address"]) for loc in locations]
            raise NotFoundError(
                f"Location '{ref}' not found" + matching.suggestion(matching.close_matches(ref, candidates))
            )
        found = repo.get_location_by_name(conn, name)
    return found


# --- reads -------------------------------------------------------------------


@database_errors("search_items")
def search_items(conn: Connection, user: AuthenticatedUser, query: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_permission(user, Permission.INVENTORY_READ)
    return repo.search_items(conn, require_text(query, "query"), check_limit(limit))


@database_errors("get_item_details")
def get_item_details(conn: Connection, user: AuthenticatedUser, item: str) -> dict[str, Any]:
    ensure_permission(user, Permission.INVENTORY_READ)
    found = resolve_item(conn, item)
    stock = repo.get_current_stock(conn, found["id"])
    total = sum((Decimal(str(row["quantity"])) for row in stock), Decimal(0))
    return {"item": found, "stock": stock, "total_quantity": total}


@database_errors("list_locations")
def list_locations(conn: Connection, user: AuthenticatedUser, active_only: bool = True) -> list[dict[str, Any]]:
    ensure_permission(user, Permission.INVENTORY_READ)
    return repo.list_locations(conn, active_only=bool(active_only))


@database_errors("get_stock_by_location")
def get_stock_by_location(
    conn: Connection, user: AuthenticatedUser, location: str, limit: int = 100
) -> dict[str, Any]:
    ensure_permission(user, Permission.INVENTORY_READ)
    loc = resolve_location(conn, location)
    return {"location": loc, "stock": repo.get_stock_by_location(conn, loc["id"], check_limit(limit))}


@database_errors("get_transaction_history")
def get_transaction_history(
    conn: Connection,
    user: AuthenticatedUser,
    item: str | None = None,
    location: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_permission(user, Permission.INVENTORY_READ)
    item_id = resolve_item(conn, item)["id"] if item is not None else None
    location_id = resolve_location(conn, location)["id"] if location is not None else None
    rows = repo.get_transactions(conn, item_id=item_id, location_id=location_id, limit=check_limit(limit))
    if has_permission(user, Permission.PROCUREMENT_READ):
        return rows
    return [_without_procurement_fields(row) for row in rows]


def _without_procurement_fields(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("reference_type") != PURCHASE_ORDER_REFERENCE:
        return row
    return {**row, **dict.fromkeys(PROCUREMENT_TRANSACTION_FIELDS)}


@database_errors("get_low_stock_items")
def get_low_stock_items(
    conn: Connection,
    user: AuthenticatedUser,
    threshold: Any = None,
    location: str | None = None,
    limit: int = 100,
    query: str | None = None,
) -> dict[str, Any]:
    """Stock rows at or below min_stock_level, or below `threshold` where that is unset.

    `query` narrows to items whose name or code contains it.
    """
    ensure_permission(user, Permission.INVENTORY_READ)
    fallback = (
        LOW_STOCK_FALLBACK_THRESHOLD
        if threshold is None
        else to_decimal(threshold, "threshold", places=3, minimum=Decimal(0), maximum=MAX_MOVEMENT_QUANTITY)
    )
    query = optional_text(query, "query", 100)
    location_id = resolve_location(conn, location)["id"] if location is not None else None
    rows = repo.list_low_stock(
        conn, float(fallback), location_id=location_id, limit=check_limit(limit), query=query
    )
    return {"fallback_threshold": fallback, "items": rows}


# --- writes ------------------------------------------------------------------


@database_errors("record_stock_movement")
def record_stock_movement(
    conn: Connection,
    user: AuthenticatedUser,
    *,
    item: str,
    location: str,
    direction: str,
    quantity: Any,
    notes: str | None = None,
) -> dict[str, Any]:
    """Receive (inbound) or remove (outbound) stock at one location, atomically with its audit record."""
    ensure_permission(user, Permission.INVENTORY_WRITE)
    try:
        move = Direction(direction)
    except ValueError:
        raise ValidationError("direction must be 'inbound' or 'outbound'") from None
    amount = to_decimal(
        quantity, "quantity", places=3, minimum=Decimal(0), maximum=MAX_MOVEMENT_QUANTITY, allow_minimum=False
    )
    notes = optional_text(notes, "notes", MAX_NOTES_LENGTH)
    signed = amount if move is Direction.INBOUND else -amount

    with atomic(conn):
        found = resolve_item(conn, item)
        if found["status"] != "active":
            raise ValidationError(f"Item '{found['item_code']}' is not active")
        loc = resolve_location(conn, location)
        if not loc["is_active"]:
            raise ValidationError(f"Location '{loc['name']}' is not active")

        now = utcnow()
        if move is Direction.INBOUND:
            repo.ensure_stock_row(conn, found["id"], loc["id"], now)
        stock = repo.lock_stock_row(conn, found["id"], loc["id"])
        before = Decimal(str(stock["quantity"])) if stock else Decimal(0)
        after = before + signed
        if stock is None or after < 0:
            raise InsufficientStockError(
                f"Cannot remove {amount} {found['unit']} of '{found['item_code']}' from "
                f"'{loc['name']}': only {before} available"
            )

        repo.update_stock_quantity(conn, stock["id"], float(after), now)
        transaction = repo.insert_transaction(
            conn,
            item_id=found["id"],
            location_id=loc["id"],
            transaction_type=move.value,
            quantity=float(signed),
            notes=notes,
            created_by=str(user.id),
            now=now,
        )
        operation_id = str(uuid.uuid4())
        audit_repo.insert_audit_record(
            conn,
            operation_id=operation_id,
            user_id=user.id,
            role=user.role.value,
            operation="record_stock_movement",
            entity_type="inventory_transaction",
            entity_id=transaction["id"],
            before_state={"item_id": found["id"], "location_id": loc["id"], "quantity": before},
            after_state={
                "transaction_id": transaction["id"],
                "item_id": found["id"],
                "item_code": found["item_code"],
                "location_id": loc["id"],
                "location_name": loc["name"],
                "direction": move.value,
                "quantity_change": signed,
                "quantity": after,
            },
            status="success",
        )

    return {
        "transaction": transaction,
        "item": {k: found[k] for k in ("id", "item_code", "name", "unit")},
        "location": {k: loc[k] for k in ("id", "name")},
        "direction": move.value,
        "quantity_before": before,
        "quantity_after": after,
        "audit_operation_id": operation_id,
    }
