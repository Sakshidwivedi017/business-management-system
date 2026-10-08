"""Data access for procurement tables (proc_*).

Write functions run inside the caller's transaction and never commit; business
rules (validation, permissions, audit) live in app.services.procurement.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from app.db.connection import fetch_all, fetch_one

_PO_SELECT = """
    SELECT po.id, po.po_number, po.status, po.placed_on,
           po.vendor_id, v.name AS vendor_name,
           po.currency, po.subtotal, po.tax_amount, po.total_amount,
           po.payment_terms, po.delivery_location_id, l.name AS delivery_location_name,
           po.ship_to_address, po.expected_delivery_date, po.expected_dispatch_date,
           po.created_by, po.created_at, po.updated_at
    FROM proc_purchase_orders po
    JOIN proc_vendors v ON v.id = po.vendor_id
    LEFT JOIN inv_locations l ON l.id = po.delivery_location_id
"""


def list_vendors(
    conn: Connection, name: str | None = None, active_only: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT id, name, code, city, payment_terms, is_active, created_at, updated_at
        FROM proc_vendors
        WHERE (CAST(:pattern AS varchar) IS NULL OR name ILIKE :pattern)
          AND (:active_only = false OR is_active)
        ORDER BY name
        LIMIT :limit
        """,
        pattern=f"%{name}%" if name else None,
        active_only=active_only,
        limit=limit,
    )


def get_vendor_by_id(conn: Connection, vendor_id: str) -> dict[str, Any] | None:
    return fetch_one(
        conn,
        "SELECT id, name, code, city, payment_terms, is_active, created_at, updated_at"
        " FROM proc_vendors WHERE id = :vendor_id",
        vendor_id=vendor_id,
    )


def list_purchase_orders(
    conn: Connection, status: str | None = None, vendor_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Purchase orders, newest first. Status values in the data: placed, partial, received."""
    return fetch_all(
        conn,
        _PO_SELECT
        + """
        WHERE (CAST(:status AS varchar) IS NULL OR po.status = :status)
          AND (CAST(:vendor_id AS varchar) IS NULL OR po.vendor_id = :vendor_id)
        ORDER BY po.placed_on DESC NULLS LAST, po.created_at DESC
        LIMIT :limit
        """,
        status=status,
        vendor_id=vendor_id,
        limit=limit,
    )


def get_purchase_order_by_id(conn: Connection, po_id: str) -> dict[str, Any] | None:
    return fetch_one(conn, _PO_SELECT + " WHERE po.id = :po_id", po_id=po_id)


def get_purchase_order_by_number(conn: Connection, po_number: str) -> dict[str, Any] | None:
    return fetch_one(conn, _PO_SELECT + " WHERE po.po_number = :po_number", po_number=po_number)


def list_po_numbers(conn: Connection) -> list[str]:
    """Every PO number, for matching a mistyped reference."""
    return [row["po_number"] for row in fetch_all(conn, "SELECT po_number FROM proc_purchase_orders")]


def get_purchase_order_lines(conn: Connection, po_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT pl.id, pl.po_id, pl.position, pl.inv_item_id,
               i.item_code, i.name AS item_name,
               pl.quantity_ordered, pl.quantity_received, pl.unit,
               pl.unit_price, pl.tax_percentage, pl.line_subtotal, pl.line_tax, pl.line_total,
               pl.is_regularised, pl.expected_delivery_date, pl.expected_dispatch_date, pl.notes
        FROM proc_po_lines pl
        LEFT JOIN inv_items i ON i.id = pl.inv_item_id
        WHERE pl.po_id = :po_id
        ORDER BY pl.position
        """,
        po_id=po_id,
    )


def get_purchase_order_receipts(conn: Connection, po_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT r.id, r.po_line_id, pl.inv_item_id, i.item_code, i.name AS item_name,
               r.quantity, r.location_id, l.name AS location_name,
               r.inv_transaction_id, r.notes, r.received_by, r.received_at
        FROM proc_po_receipts r
        JOIN proc_po_lines pl ON pl.id = r.po_line_id
        LEFT JOIN inv_items i ON i.id = pl.inv_item_id
        LEFT JOIN inv_locations l ON l.id = r.location_id
        WHERE pl.po_id = :po_id
        ORDER BY r.received_at
        """,
        po_id=po_id,
    )


def get_payment_tranches(conn: Connection, po_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT id, po_id, sequence, stage, percentage, amount, is_advance, blocks_dispatch,
               due_basis, due_offset_days, source, cleared_at, cleared_by
        FROM proc_po_payment_tranches
        WHERE po_id = :po_id
        ORDER BY sequence
        """,
        po_id=po_id,
    )


# --- writes (caller owns the transaction) ------------------------------------

# Arbitrary fixed key: every PO-number allocation in this app serialises on it.
_PO_NUMBER_LOCK_KEY = 7_340_001


def lock_po_numbering(conn: Connection) -> None:
    """Transaction-scoped advisory lock; released automatically at commit/rollback."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PO_NUMBER_LOCK_KEY})


def get_max_po_sequence(conn: Connection, year: int) -> int:
    """Highest NNNN among PO-<year>-NNNN numbers, or 0. Call while holding the numbering lock."""
    return conn.execute(
        text(
            """
            SELECT COALESCE(MAX(CAST(substring(po_number FROM 9) AS integer)), 0)
            FROM proc_purchase_orders
            WHERE po_number ~ ('^PO-' || CAST(:year AS text) || '-[0-9]{4}$')
            """
        ),
        {"year": year},
    ).scalar_one()


def insert_purchase_order(
    conn: Connection,
    *,
    po_id: str,
    po_number: str,
    vendor_id: str,
    status: str,
    placed_on: date,
    currency: str,
    subtotal: Decimal,
    tax_amount: Decimal,
    total_amount: Decimal,
    payment_terms: str | None,
    delivery_location_id: str | None,
    created_by: str,
    now: datetime,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO proc_purchase_orders
                (id, po_number, vendor_id, status, placed_on, currency, subtotal, tax_amount,
                 total_amount, payment_terms, delivery_location_id, created_by, created_at, updated_at)
            VALUES (:po_id, :po_number, :vendor_id, :status, :placed_on, :currency, :subtotal,
                    :tax_amount, :total_amount, :payment_terms, :delivery_location_id, :created_by,
                    :now, :now)
            """
        ),
        {
            "po_id": po_id, "po_number": po_number, "vendor_id": vendor_id, "status": status,
            "placed_on": placed_on, "currency": currency, "subtotal": subtotal,
            "tax_amount": tax_amount, "total_amount": total_amount, "payment_terms": payment_terms,
            "delivery_location_id": delivery_location_id, "created_by": created_by, "now": now,
        },
    )


def lock_purchase_order_lines(conn: Connection, po_id: str) -> list[dict[str, Any]]:
    """Row-lock every line of one PO, in position order, until the transaction ends."""
    return fetch_all(
        conn,
        """
        SELECT pl.id, pl.po_id, pl.position, pl.inv_item_id, i.item_code, i.name AS item_name,
               COALESCE(pl.unit, i.unit) AS unit, pl.quantity_ordered, pl.quantity_received, pl.unit_price
        FROM proc_po_lines pl
        LEFT JOIN inv_items i ON i.id = pl.inv_item_id
        WHERE pl.po_id = :po_id
        ORDER BY pl.position, pl.id
        FOR UPDATE OF pl
        """,
        po_id=po_id,
    )


def insert_po_receipt(
    conn: Connection,
    *,
    receipt_id: str,
    po_line_id: str,
    quantity: Decimal,
    location_id: str,
    notes: str | None,
    inv_transaction_id: str,
    received_by: str,
    now: datetime,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO proc_po_receipts
                (id, po_line_id, quantity, location_id, notes, inv_transaction_id, received_by, received_at)
            VALUES (:receipt_id, :po_line_id, :quantity, :location_id, :notes, :inv_transaction_id,
                    :received_by, :now)
            """
        ),
        {
            "receipt_id": receipt_id, "po_line_id": po_line_id, "quantity": quantity, "location_id": location_id,
            "notes": notes, "inv_transaction_id": inv_transaction_id, "received_by": received_by, "now": now,
        },
    )


def update_po_line_received(conn: Connection, line_id: str, quantity_received: Decimal, now: datetime) -> None:
    conn.execute(
        text("UPDATE proc_po_lines SET quantity_received = :quantity_received, updated_at = :now WHERE id = :id"),
        {"id": line_id, "quantity_received": quantity_received, "now": now},
    )


def update_purchase_order_status(conn: Connection, po_id: str, status: str, now: datetime) -> None:
    conn.execute(
        text("UPDATE proc_purchase_orders SET status = :status, updated_at = :now WHERE id = :id"),
        {"id": po_id, "status": status, "now": now},
    )


def insert_po_line(
    conn: Connection,
    *,
    line_id: str,
    po_id: str,
    position: int,
    item_id: str,
    unit: str,
    quantity: Decimal,
    unit_price: Decimal,
    tax_percentage: Decimal,
    line_subtotal: Decimal,
    line_tax: Decimal,
    line_total: Decimal,
    notes: str | None,
    now: datetime,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO proc_po_lines
                (id, po_id, inv_item_id, quantity_ordered, quantity_received, unit, unit_price,
                 tax_percentage, line_subtotal, line_tax, line_total, notes, position,
                 created_at, updated_at)
            VALUES (:line_id, :po_id, :item_id, :quantity, 0, :unit, :unit_price, :tax_percentage,
                    :line_subtotal, :line_tax, :line_total, :notes, :position, :now, :now)
            """
        ),
        {
            "line_id": line_id, "po_id": po_id, "item_id": item_id, "quantity": quantity,
            "unit": unit, "unit_price": unit_price, "tax_percentage": tax_percentage,
            "line_subtotal": line_subtotal, "line_tax": line_tax, "line_total": line_total,
            "notes": notes, "position": position, "now": now,
        },
    )
