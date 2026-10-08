"""Procurement business operations.

All money is computed here with Decimal, matching the rounding already present
in the data: line_subtotal = round(qty * price, 2), line_tax =
round(line_subtotal * tax% / 100, 2), header totals = sums of the lines.
Caller-supplied totals are never accepted.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission
from app.db.connection import atomic
from app.db.repositories import audit as audit_repo
from app.db.repositories import inventory as inv_repo
from app.db.repositories import procurement as repo
from app.services import matching
from app.services.errors import ConflictError, NotFoundError, ValidationError, database_errors
from app.services.inventory import PURCHASE_ORDER_REFERENCE, resolve_item, resolve_location, utcnow
from app.services.validation import check_limit, optional_text, require_text, to_decimal

PO_STATUSES = ("placed", "partial", "received")
PO_STATUS_ON_CREATE = "placed"
OPEN_PO_STATUSES = ("placed", "partial")
MAX_PO_LINES = 50
MAX_RECEIPT_LINES = 50
MAX_LINE_POSITION = 10_000

_CENT = Decimal("0.01")
_MAX_QUANTITY = Decimal("99999999999.999")  # numeric(14,3)
_MAX_AMOUNT = Decimal("999999999999.99")  # numeric(14,2)


@dataclass(frozen=True)
class PurchaseOrderLineInput:
    item: str  # item id or item code
    quantity: Any
    unit_price: Any
    tax_percentage: Any
    notes: str | None = None


@dataclass(frozen=True)
class PurchaseReceiptLineInput:
    purchase_order: str  # PO number or PO id
    item: str  # item code or item id of the PO line
    quantity: Any  # in the PO line's unit
    line_position: int | None = None  # needed only when the item is on more than one line of the PO


# --- reads -------------------------------------------------------------------


@database_errors("list_vendors")
def list_vendors(
    conn: Connection,
    user: AuthenticatedUser,
    name: str | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_permission(user, Permission.PROCUREMENT_READ)
    return repo.list_vendors(
        conn, name=optional_text(name, "name", 100), active_only=bool(active_only), limit=check_limit(limit)
    )


@database_errors("get_vendor")
def get_vendor(conn: Connection, user: AuthenticatedUser, vendor_id: str) -> dict[str, Any]:
    ensure_permission(user, Permission.PROCUREMENT_READ)
    vendor = repo.get_vendor_by_id(conn, require_text(vendor_id, "vendor_id"))
    if vendor is None:
        raise NotFoundError(f"Vendor '{vendor_id}' not found")
    return vendor


@database_errors("list_purchase_orders")
def list_purchase_orders(
    conn: Connection,
    user: AuthenticatedUser,
    status: str | None = None,
    vendor_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_permission(user, Permission.PROCUREMENT_READ)
    if status is not None and status not in PO_STATUSES:
        raise ValidationError(f"status must be one of: {', '.join(PO_STATUSES)}")
    return repo.list_purchase_orders(
        conn, status=status, vendor_id=optional_text(vendor_id, "vendor_id", 100), limit=check_limit(limit)
    )


@database_errors("get_purchase_order")
def get_purchase_order(conn: Connection, user: AuthenticatedUser, purchase_order: str) -> dict[str, Any]:
    """Header, lines, receipts and payment tranches for a PO given by id or PO number."""
    ensure_permission(user, Permission.PROCUREMENT_READ)
    return _load_purchase_order(conn, require_text(purchase_order, "purchase_order"))


def _find_purchase_order(conn: Connection, ref: str) -> dict[str, Any]:
    """A PO by id or PO number (case and punctuation aside). Otherwise suggest, never guess."""
    header = repo.get_purchase_order_by_id(conn, ref) or repo.get_purchase_order_by_number(conn, ref)
    if header is None:
        numbers = repo.list_po_numbers(conn)
        number = matching.same_key(ref, numbers)
        if number is None:
            labels = matching.close_matches(ref, [(n, None) for n in numbers], number_suffix=True)
            raise NotFoundError(f"Purchase order '{ref}' not found" + matching.suggestion(labels))
        header = repo.get_purchase_order_by_number(conn, number)
    return header


def _load_purchase_order(conn: Connection, ref: str) -> dict[str, Any]:
    header = _find_purchase_order(conn, ref)
    return {
        "purchase_order": header,
        "lines": repo.get_purchase_order_lines(conn, header["id"]),
        "receipts": repo.get_purchase_order_receipts(conn, header["id"]),
        "payment_tranches": repo.get_payment_tranches(conn, header["id"]),
    }


# --- writes ------------------------------------------------------------------


def calculate_line_amounts(quantity: Decimal, unit_price: Decimal, tax_percentage: Decimal) -> dict[str, Decimal]:
    subtotal = (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP)
    tax = (subtotal * tax_percentage / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    return {"line_subtotal": subtotal, "line_tax": tax, "line_total": subtotal + tax}


def _parse_line(index: int, raw: PurchaseOrderLineInput | Mapping[str, Any]) -> dict[str, Any]:
    label = f"lines[{index}]"
    if isinstance(raw, Mapping):
        try:
            raw = PurchaseOrderLineInput(**raw)
        except TypeError:
            raise ValidationError(
                f"{label} must have item, quantity, unit_price, tax_percentage and optional notes"
            ) from None
    elif not isinstance(raw, PurchaseOrderLineInput):
        raise ValidationError(f"{label} is not a valid purchase order line")
    return {
        "item": require_text(raw.item, f"{label}.item"),
        "quantity": to_decimal(
            raw.quantity, f"{label}.quantity", places=3, minimum=Decimal(0), maximum=_MAX_QUANTITY,
            allow_minimum=False,
        ),
        "unit_price": to_decimal(
            raw.unit_price, f"{label}.unit_price", places=2, minimum=Decimal(0), maximum=_MAX_AMOUNT
        ),
        "tax_percentage": to_decimal(
            raw.tax_percentage, f"{label}.tax_percentage", places=2, minimum=Decimal(0), maximum=Decimal(100)
        ),
        "notes": optional_text(raw.notes, f"{label}.notes", 500),
    }


@database_errors("create_purchase_order")
def create_purchase_order(
    conn: Connection,
    user: AuthenticatedUser,
    *,
    vendor_id: str,
    lines: Sequence[PurchaseOrderLineInput | Mapping[str, Any]],
    delivery_location: str | None = None,
    payment_terms: str | None = None,
    currency: str = "INR",
) -> dict[str, Any]:
    """Create a placed PO (header + lines + audit record) in one transaction."""
    ensure_permission(user, Permission.PROCUREMENT_WRITE)
    vendor_ref = require_text(vendor_id, "vendor_id")
    if isinstance(lines, (str, bytes, Mapping)) or not isinstance(lines, Sequence) or not lines:
        raise ValidationError("lines must be a non-empty list")
    if len(lines) > MAX_PO_LINES:
        raise ValidationError(f"A purchase order may have at most {MAX_PO_LINES} lines")
    parsed = [_parse_line(i, line) for i, line in enumerate(lines)]
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.isupper()):
        raise ValidationError("currency must be a 3-letter uppercase code, e.g. INR")
    payment_terms = optional_text(payment_terms, "payment_terms", 200)

    for line in parsed:
        line.update(calculate_line_amounts(line["quantity"], line["unit_price"], line["tax_percentage"]))
    subtotal = sum((line["line_subtotal"] for line in parsed), Decimal("0.00"))
    tax_amount = sum((line["line_tax"] for line in parsed), Decimal("0.00"))
    total_amount = subtotal + tax_amount
    if total_amount > _MAX_AMOUNT:
        raise ValidationError("Purchase order total is too large")

    with atomic(conn):
        vendor = repo.get_vendor_by_id(conn, vendor_ref)
        if vendor is None:
            raise NotFoundError(f"Vendor '{vendor_ref}' not found")
        if not vendor["is_active"]:
            raise ValidationError(f"Vendor '{vendor['name']}' is not active")

        location_id = None
        if delivery_location is not None:
            loc = resolve_location(conn, delivery_location)
            if not loc["is_active"]:
                raise ValidationError(f"Location '{loc['name']}' is not active")
            location_id = loc["id"]

        for line in parsed:
            item = resolve_item(conn, line["item"])
            if item["status"] != "active":
                raise ValidationError(f"Item '{item['item_code']}' is not active")
            line["item_row"] = item

        now = utcnow()
        placed_on = now.date()
        # Serialise numbering: concurrent creators wait here until this transaction ends.
        repo.lock_po_numbering(conn)
        sequence = repo.get_max_po_sequence(conn, placed_on.year) + 1
        if sequence > 9999:
            raise ConflictError(f"PO number range for {placed_on.year} is exhausted")
        po_number = f"PO-{placed_on.year}-{sequence:04d}"
        po_id = str(uuid.uuid4())

        repo.insert_purchase_order(
            conn,
            po_id=po_id,
            po_number=po_number,
            vendor_id=vendor["id"],
            status=PO_STATUS_ON_CREATE,
            placed_on=placed_on,
            currency=currency,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=total_amount,
            payment_terms=payment_terms,
            delivery_location_id=location_id,
            created_by=str(user.id),
            now=now,
        )
        for position, line in enumerate(parsed):
            repo.insert_po_line(
                conn,
                line_id=str(uuid.uuid4()),
                po_id=po_id,
                position=position,
                item_id=line["item_row"]["id"],
                unit=line["item_row"]["unit"],
                quantity=line["quantity"],
                unit_price=line["unit_price"],
                tax_percentage=line["tax_percentage"],
                line_subtotal=line["line_subtotal"],
                line_tax=line["line_tax"],
                line_total=line["line_total"],
                notes=line["notes"],
                now=now,
            )

        operation_id = str(uuid.uuid4())
        audit_repo.insert_audit_record(
            conn,
            operation_id=operation_id,
            user_id=user.id,
            role=user.role.value,
            operation="create_purchase_order",
            entity_type="purchase_order",
            entity_id=po_id,
            before_state=None,
            after_state={
                "po_id": po_id,
                "po_number": po_number,
                "status": PO_STATUS_ON_CREATE,
                "vendor_id": vendor["id"],
                "vendor_name": vendor["name"],
                "currency": currency,
                "subtotal": subtotal,
                "tax_amount": tax_amount,
                "total_amount": total_amount,
                "lines": [
                    {
                        "item_id": line["item_row"]["id"],
                        "item_code": line["item_row"]["item_code"],
                        "quantity": line["quantity"],
                        "unit_price": line["unit_price"],
                        "tax_percentage": line["tax_percentage"],
                        "line_total": line["line_total"],
                    }
                    for line in parsed
                ],
            },
            status="success",
        )
        result = _load_purchase_order(conn, po_id)

    result["audit_operation_id"] = operation_id
    return result


# --- goods receipts ----------------------------------------------------------


def purchase_order_status(lines: Sequence[Mapping[str, Any]]) -> str:
    """The existing convention: received once every line is fully received, partial once anything is."""
    if all(line["quantity_received"] >= line["quantity_ordered"] for line in lines):
        return "received"
    if any(line["quantity_received"] > 0 for line in lines):
        return "partial"
    return "placed"


def _plain(number: Decimal) -> str:
    return format(number.normalize(), "f")


def _parse_receipt_line(index: int, raw: PurchaseReceiptLineInput | Mapping[str, Any]) -> dict[str, Any]:
    label = f"lines[{index}]"
    if isinstance(raw, Mapping):
        try:
            raw = PurchaseReceiptLineInput(**raw)
        except TypeError:
            raise ValidationError(
                f"{label} must have purchase_order, item, quantity and optional line_position"
            ) from None
    elif not isinstance(raw, PurchaseReceiptLineInput):
        raise ValidationError(f"{label} is not a valid receipt line")
    position = raw.line_position
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= MAX_LINE_POSITION
    ):
        raise ValidationError(f"{label}.line_position must be an integer between 0 and {MAX_LINE_POSITION}")
    return {
        "purchase_order": require_text(raw.purchase_order, f"{label}.purchase_order"),
        "item": require_text(raw.item, f"{label}.item"),
        "quantity": to_decimal(
            raw.quantity, f"{label}.quantity", places=3, minimum=Decimal(0), maximum=_MAX_QUANTITY,
            allow_minimum=False,
        ),
        "line_position": position,
    }


def _match_line(
    po: Mapping[str, Any], lines: Sequence[dict[str, Any]], item: Mapping[str, Any], position: int | None
) -> dict[str, Any]:
    """The one PO line for this item (and position, if given). Ambiguity is rejected, never guessed."""
    candidates = [line for line in lines if line["inv_item_id"] == item["id"]]
    if position is not None:
        candidates = [line for line in candidates if line["position"] == position]
    if not candidates:
        where = f" at line position {position}" if position is not None else ""
        raise NotFoundError(f"Item '{item['item_code']}' is not on purchase order '{po['po_number']}'{where}")
    if len(candidates) > 1:
        positions = ", ".join(str(line["position"]) for line in candidates)
        raise ValidationError(
            f"Item '{item['item_code']}' is on more than one line of purchase order '{po['po_number']}' "
            f"(line positions {positions}); say which line_position was received"
        )
    return candidates[0]


@database_errors("record_purchase_receipt")
def record_purchase_receipt(
    conn: Connection,
    user: AuthenticatedUser,
    *,
    location: str,
    lines: Sequence[PurchaseReceiptLineInput | Mapping[str, Any]],
    notes: str | None = None,
) -> dict[str, Any]:
    """Record goods received against PO lines at one location, all in one transaction.

    Per line, as historical receipts are stored: stock at the location goes up, an inbound
    inv_transactions row references the PO (unit cost from the PO line, currency from the PO),
    a proc_po_receipts row links line, location and transaction, and the line's quantity_received
    grows. Each PO's status is then derived from its lines, and one audit record covers it all.
    Quantities are in the PO line's unit; over-receipt is rejected (no regularisation here).
    """
    ensure_permission(user, Permission.PROCUREMENT_WRITE)
    if isinstance(lines, (str, bytes, Mapping)) or not isinstance(lines, Sequence) or not lines:
        raise ValidationError("lines must be a non-empty list")
    if len(lines) > MAX_RECEIPT_LINES:
        raise ValidationError(f"A receipt may have at most {MAX_RECEIPT_LINES} lines")
    parsed = [_parse_receipt_line(i, line) for i, line in enumerate(lines)]
    notes = optional_text(notes, "notes", 500)

    with atomic(conn):
        loc = resolve_location(conn, location)
        if not loc["is_active"]:
            raise ValidationError(f"Location '{loc['name']}' is not active")

        orders: dict[str, dict[str, Any]] = {}  # by PO id, in the order the request names them
        for line in parsed:
            header = _find_purchase_order(conn, line["purchase_order"])
            orders.setdefault(header["id"], header)
            line["po_id"] = header["id"]
            line["item_row"] = resolve_item(conn, line["item"])

        # Fixed lock order (PO id, then line position; then stock rows by item id), so concurrent receipts
        # wait for each other instead of deadlocking. Quantities and statuses are read only under these locks.
        po_lines = {}
        for po_id in sorted(orders):
            po_lines[po_id] = repo.lock_purchase_order_lines(conn, po_id)
            orders[po_id] = repo.get_purchase_order_by_id(conn, po_id)  # current, now that its lines are locked

        status_before = {po_id: po["status"] for po_id, po in orders.items()}
        matched: set[str] = set()
        for line in parsed:
            po, item = orders[line["po_id"]], line["item_row"]
            if po["status"] == "received":
                raise ValidationError(f"Purchase order '{po['po_number']}' is already fully received")
            po_line = _match_line(po, po_lines[po["id"]], item, line["line_position"])
            if po_line["id"] in matched:
                raise ValidationError(
                    f"Line {po_line['position']} of purchase order '{po['po_number']}' is listed more than once"
                )
            matched.add(po_line["id"])
            remaining = po_line["quantity_ordered"] - po_line["quantity_received"]
            unit = po_line["unit"] or ""
            if remaining <= 0:
                raise ValidationError(
                    f"Item '{item['item_code']}' on purchase order '{po['po_number']}' is already fully received"
                )
            if line["quantity"] > remaining:
                raise ValidationError(
                    f"Cannot receive {_plain(line['quantity'])} {unit} of '{item['item_code']}' against "
                    f"'{po['po_number']}': only {_plain(remaining)} {unit} remain to be received"
                )
            line["po_line"] = po_line

        now = utcnow()
        stock: dict[str, dict[str, Any]] = {}
        for item_id in sorted({line["item_row"]["id"] for line in parsed}):
            inv_repo.ensure_stock_row(conn, item_id, loc["id"], now)
            row = inv_repo.lock_stock_row(conn, item_id, loc["id"])
            assert row is not None
            stock[item_id] = {"id": row["id"], "quantity": Decimal(str(row["quantity"]))}

        receipts = []
        for line in parsed:
            po, po_line, item, quantity = orders[line["po_id"]], line["po_line"], line["item_row"], line["quantity"]
            held = stock[item["id"]]
            stock_before = held["quantity"]
            held["quantity"] = stock_before + quantity
            inv_repo.update_stock_quantity(conn, held["id"], float(held["quantity"]), now)
            transaction = inv_repo.insert_transaction(
                conn,
                item_id=item["id"],
                location_id=loc["id"],
                transaction_type="inbound",
                quantity=float(quantity),
                notes=notes,
                created_by=str(user.id),
                now=now,
                reference=(PURCHASE_ORDER_REFERENCE, po["id"]),
                unit_cost=float(po_line["unit_price"]),
                currency=po["currency"],
            )
            receipt_id = str(uuid.uuid4())
            repo.insert_po_receipt(
                conn,
                receipt_id=receipt_id,
                po_line_id=po_line["id"],
                quantity=quantity,
                location_id=loc["id"],
                notes=notes,
                inv_transaction_id=transaction["id"],
                received_by=str(user.id),
                now=now,
            )
            received_before = po_line["quantity_received"]
            po_line["quantity_received"] = received_before + quantity
            repo.update_po_line_received(conn, po_line["id"], po_line["quantity_received"], now)
            receipts.append(
                {
                    "receipt_id": receipt_id,
                    "transaction_id": transaction["id"],
                    "po_id": po["id"],
                    "po_number": po["po_number"],
                    "po_line_id": po_line["id"],
                    "line_position": po_line["position"],
                    "item_id": item["id"],
                    "item_code": item["item_code"],
                    "item_name": item["name"],
                    "unit": po_line["unit"],
                    "quantity": quantity,
                    "quantity_ordered": po_line["quantity_ordered"],
                    "received_before": received_before,
                    "received_after": po_line["quantity_received"],
                    "stock_before": stock_before,
                    "stock_after": held["quantity"],
                }
            )

        purchase_orders = []
        for po_id, po in orders.items():
            status = purchase_order_status(po_lines[po_id])
            repo.update_purchase_order_status(conn, po_id, status, now)
            purchase_orders.append(
                {"po_id": po_id, "po_number": po["po_number"], "status_before": status_before[po_id],
                 "status_after": status}
            )

        operation_id = str(uuid.uuid4())
        audit_repo.insert_audit_record(
            conn,
            operation_id=operation_id,
            user_id=user.id,
            role=user.role.value,
            operation="record_purchase_receipt",
            entity_type="purchase_receipt",
            entity_id=receipts[0]["receipt_id"],
            before_state={
                "location_id": loc["id"],
                "lines": [
                    {"po_line_id": r["po_line_id"], "quantity_received": r["received_before"],
                     "stock_quantity": r["stock_before"]}
                    for r in receipts
                ],
                "purchase_orders": [{"po_id": p["po_id"], "status": p["status_before"]} for p in purchase_orders],
            },
            after_state={
                "location_id": loc["id"],
                "location_name": loc["name"],
                "receipts": receipts,
                "purchase_orders": purchase_orders,
            },
            status="success",
        )

    return {
        "location": {k: loc[k] for k in ("id", "name")},
        "receipts": receipts,
        "purchase_orders": purchase_orders,
        "audit_operation_id": operation_id,
    }
