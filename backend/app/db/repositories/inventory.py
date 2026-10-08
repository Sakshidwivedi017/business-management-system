"""Data access for inventory tables (inv_*).

Write functions run inside the caller's transaction and never commit; business
rules (validation, permissions, audit) live in app.services.inventory.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from app.db.connection import fetch_all, fetch_one

_ITEM_SELECT = """
    SELECT i.id, i.item_code, i.name, i.status, i.unit, i.hsn_code, i.is_spare,
           i.notes, i.specifications, i.alt_units,
           i.category_id, c.name AS category_name,
           i.sub_category_id, sc.name AS sub_category_name,
           i.sub_category_2_id, sc2.name AS sub_category_2_name
    FROM inv_items i
    LEFT JOIN inv_categories c ON c.id = i.category_id
    LEFT JOIN inv_sub_categories sc ON sc.id = i.sub_category_id
    LEFT JOIN inv_sub_categories_2 sc2 ON sc2.id = i.sub_category_2_id
"""

_STOCK_SELECT = """
    SELECT s.item_id, i.item_code, i.name AS item_name, i.unit,
           s.location_id, l.name AS location_name,
           s.quantity, s.min_stock_level, s.max_stock_level,
           s.last_transaction_at, s.updated_at
    FROM inv_current_stock s
    JOIN inv_items i ON i.id = s.item_id
    JOIN inv_locations l ON l.id = s.location_id
"""


def get_item_by_id(conn: Connection, item_id: str) -> dict[str, Any] | None:
    return fetch_one(conn, _ITEM_SELECT + " WHERE i.id = :item_id", item_id=item_id)


def get_item_by_code(conn: Connection, item_code: str) -> dict[str, Any] | None:
    return fetch_one(conn, _ITEM_SELECT + " WHERE i.item_code = :item_code", item_code=item_code)


def list_item_codes(conn: Connection) -> list[tuple[str, str | None]]:
    """(item_code, name) of every item, for matching a mistyped reference."""
    return [(row["item_code"], row["name"]) for row in fetch_all(conn, "SELECT item_code, name FROM inv_items")]


def search_items(conn: Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Case-insensitive substring match on item name or item code."""
    return fetch_all(
        conn,
        _ITEM_SELECT
        + " WHERE i.name ILIKE :pattern OR i.item_code ILIKE :pattern"
        + " ORDER BY i.item_code LIMIT :limit",
        pattern=f"%{query}%",
        limit=limit,
    )


def list_locations(conn: Connection, active_only: bool = True) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        "SELECT id, name, address, is_active FROM inv_locations"
        " WHERE (:active_only = false OR is_active) ORDER BY name",
        active_only=active_only,
    )


def get_location_by_id(conn: Connection, location_id: str) -> dict[str, Any] | None:
    return fetch_one(
        conn, "SELECT id, name, address, is_active FROM inv_locations WHERE id = :id", id=location_id
    )


def get_location_by_name(conn: Connection, name: str) -> dict[str, Any] | None:
    return fetch_one(
        conn, "SELECT id, name, address, is_active FROM inv_locations WHERE name = :name", name=name
    )


def get_current_stock(conn: Connection, item_id: str) -> list[dict[str, Any]]:
    """Stock rows for one item, one per location."""
    return fetch_all(conn, _STOCK_SELECT + " WHERE s.item_id = :item_id ORDER BY l.name", item_id=item_id)


def get_stock_by_location(conn: Connection, location_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        _STOCK_SELECT + " WHERE s.location_id = :location_id ORDER BY i.item_code LIMIT :limit",
        location_id=location_id,
        limit=limit,
    )


def get_transactions(
    conn: Connection,
    item_id: str | None = None,
    location_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Transaction history, newest first, optionally filtered by item and/or location."""
    return fetch_all(
        conn,
        """
        SELECT t.id, t.item_id, i.item_code, i.name AS item_name,
               t.location_id, l.name AS location_name,
               t.transaction_type, t.quantity, t.unit_cost, t.currency,
               t.reference_type, t.reference_id, t.transfer_location_id,
               t.issued_to_id, it.name AS issued_to_name,
               t.notes, t.created_by, t.created_at
        FROM inv_transactions t
        JOIN inv_items i ON i.id = t.item_id
        LEFT JOIN inv_locations l ON l.id = t.location_id
        LEFT JOIN inv_issued_to_targets it ON it.id = t.issued_to_id
        WHERE (CAST(:item_id AS varchar) IS NULL OR t.item_id = :item_id)
          AND (CAST(:location_id AS varchar) IS NULL OR t.location_id = :location_id)
        ORDER BY t.created_at DESC
        LIMIT :limit
        """,
        item_id=item_id,
        location_id=location_id,
        limit=limit,
    )


def list_low_stock(
    conn: Connection,
    fallback_threshold: float,
    location_id: str | None = None,
    limit: int = 100,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Active items at active locations at or below their threshold.

    The threshold is min_stock_level when set, otherwise fallback_threshold.
    `query` narrows to items whose name or code contains it (case-insensitive).
    """
    return fetch_all(
        conn,
        """
        SELECT s.item_id, i.item_code, i.name AS item_name, i.unit,
               s.location_id, l.name AS location_name,
               s.quantity, s.min_stock_level,
               COALESCE(s.min_stock_level, :fallback) AS effective_threshold,
               CASE WHEN s.min_stock_level IS NULL THEN 'fallback' ELSE 'min_stock_level' END
                   AS threshold_source,
               s.last_transaction_at
        FROM inv_current_stock s
        JOIN inv_items i ON i.id = s.item_id
        JOIN inv_locations l ON l.id = s.location_id
        WHERE i.status = 'active' AND l.is_active
          AND (CAST(:location_id AS varchar) IS NULL OR s.location_id = :location_id)
          AND (CAST(:pattern AS varchar) IS NULL OR i.name ILIKE :pattern OR i.item_code ILIKE :pattern)
          AND s.quantity <= COALESCE(s.min_stock_level, :fallback)
        ORDER BY s.quantity, i.item_code
        LIMIT :limit
        """,
        fallback=fallback_threshold,
        location_id=location_id,
        pattern=None if query is None else f"%{query}%",
        limit=limit,
    )


# --- writes (caller owns the transaction) ------------------------------------


def ensure_stock_row(conn: Connection, item_id: str, location_id: str, now: datetime) -> None:
    """Create a zero-quantity stock row if none exists; safe under concurrency."""
    conn.execute(
        text(
            """
            INSERT INTO inv_current_stock (id, item_id, location_id, quantity, updated_at)
            VALUES (:id, :item_id, :location_id, 0, :now)
            ON CONFLICT (item_id, location_id) DO NOTHING
            """
        ),
        {"id": str(uuid.uuid4()), "item_id": item_id, "location_id": location_id, "now": now},
    )


def lock_stock_row(conn: Connection, item_id: str, location_id: str) -> dict[str, Any] | None:
    """Row-lock the stock row until the transaction ends."""
    return fetch_one(
        conn,
        "SELECT id, quantity FROM inv_current_stock"
        " WHERE item_id = :item_id AND location_id = :location_id FOR UPDATE",
        item_id=item_id,
        location_id=location_id,
    )


def update_stock_quantity(conn: Connection, stock_id: str, quantity: float, now: datetime) -> None:
    conn.execute(
        text(
            "UPDATE inv_current_stock SET quantity = :quantity, last_transaction_at = :now,"
            " updated_at = :now WHERE id = :id"
        ),
        {"id": stock_id, "quantity": quantity, "now": now},
    )


def insert_transaction(
    conn: Connection,
    *,
    item_id: str,
    location_id: str,
    transaction_type: str,
    quantity: float,
    notes: str | None,
    created_by: str,
    now: datetime,
    reference: tuple[str, str] | None = None,
    unit_cost: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """`reference` is (reference_type, reference_id). Without a currency the column default applies."""
    columns = "id, item_id, location_id, transaction_type, quantity, reference_type, reference_id, unit_cost, notes"
    values = ":id, :item_id, :location_id, :transaction_type, :quantity, :reference_type, :reference_id, :unit_cost, :notes"
    extra: dict[str, Any] = {}
    if currency is not None:
        columns, values, extra = columns + ", currency", values + ", :currency", {"currency": currency}
    row = fetch_one(
        conn,
        f"""
        INSERT INTO inv_transactions ({columns}, created_by, created_at)
        VALUES ({values}, :created_by, :now)
        RETURNING id, item_id, location_id, transaction_type, quantity, reference_type, reference_id,
                  unit_cost, currency, notes, created_by, created_at
        """,
        id=str(uuid.uuid4()),
        item_id=item_id,
        location_id=location_id,
        transaction_type=transaction_type,
        quantity=quantity,
        reference_type=reference[0] if reference else None,
        reference_id=reference[1] if reference else None,
        unit_cost=unit_cost,
        notes=notes,
        created_by=created_by,
        now=now,
        **extra,
    )
    assert row is not None
    return row
