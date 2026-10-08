"""Read-only aggregate queries backing the owner summaries."""

from typing import Any

from sqlalchemy import Connection

from app.db.connection import fetch_all, fetch_one


def item_counts(conn: Connection) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT count(*) AS total_items,
               count(*) FILTER (WHERE status = 'active') AS active_items,
               (SELECT count(*) FROM inv_locations WHERE is_active) AS active_locations
        FROM inv_items
        """,
    )
    assert row is not None
    return row


def stock_by_location(conn: Connection) -> list[dict[str, Any]]:
    """Per active location: how many active items are in stock vs at/below zero."""
    return fetch_all(
        conn,
        """
        SELECT l.id AS location_id, l.name AS location_name,
               count(*) AS stocked_items,
               count(*) FILTER (WHERE s.quantity > 0) AS items_in_stock,
               count(*) FILTER (WHERE s.quantity <= 0) AS items_out_of_stock
        FROM inv_current_stock s
        JOIN inv_locations l ON l.id = s.location_id
        JOIN inv_items i ON i.id = s.item_id
        WHERE l.is_active AND i.status = 'active'
        GROUP BY l.id, l.name
        ORDER BY l.name
        """,
    )


def stock_by_category(conn: Connection) -> list[dict[str, Any]]:
    """Per category: active items, and how many hold stock > 0 at any active location."""
    return fetch_all(
        conn,
        """
        SELECT c.id AS category_id, COALESCE(c.name, 'Uncategorised') AS category_name,
               count(*) AS active_items,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM inv_current_stock s
                   JOIN inv_locations l ON l.id = s.location_id
                   WHERE s.item_id = i.id AND l.is_active AND s.quantity > 0
               )) AS items_in_stock
        FROM inv_items i
        LEFT JOIN inv_categories c ON c.id = i.category_id
        WHERE i.status = 'active'
        GROUP BY c.id, c.name
        ORDER BY category_name
        """,
    )


def low_stock_count(conn: Connection, fallback_threshold: float) -> int:
    row = fetch_one(
        conn,
        """
        SELECT count(*) AS n
        FROM inv_current_stock s
        JOIN inv_items i ON i.id = s.item_id
        JOIN inv_locations l ON l.id = s.location_id
        WHERE i.status = 'active' AND l.is_active
          AND s.quantity <= COALESCE(s.min_stock_level, :fallback)
        """,
        fallback=fallback_threshold,
    )
    assert row is not None
    return row["n"]


def recent_transaction_counts(conn: Connection, days: int) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT transaction_type, count(*) AS transactions
        FROM inv_transactions
        WHERE created_at >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
        GROUP BY transaction_type
        ORDER BY transaction_type
        """,
        days=days,
    )


def purchase_orders_by_status(conn: Connection) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT status, currency, count(*) AS purchase_orders, sum(total_amount) AS total_amount
        FROM proc_purchase_orders
        GROUP BY status, currency
        ORDER BY status, currency
        """,
    )


def open_purchase_orders(conn: Connection, open_statuses: list[str]) -> list[dict[str, Any]]:
    """Open POs per currency, plus how many of their lines still await delivery."""
    return fetch_all(
        conn,
        """
        SELECT po.currency, count(*) AS purchase_orders, sum(po.total_amount) AS total_amount,
               sum((SELECT count(*) FROM proc_po_lines pl
                    WHERE pl.po_id = po.id AND pl.quantity_received < pl.quantity_ordered))
                   AS lines_pending_delivery
        FROM proc_purchase_orders po
        WHERE po.status = ANY(:open_statuses)
        GROUP BY po.currency
        ORDER BY po.currency
        """,
        open_statuses=open_statuses,
    )


def top_vendors_by_spend(conn: Connection, limit: int) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """
        SELECT v.id AS vendor_id, v.name AS vendor_name, po.currency,
               count(*) AS purchase_orders, sum(po.total_amount) AS total_amount
        FROM proc_purchase_orders po
        JOIN proc_vendors v ON v.id = po.vendor_id
        GROUP BY v.id, v.name, po.currency
        ORDER BY sum(po.total_amount) DESC, v.name
        LIMIT :limit
        """,
        limit=limit,
    )


def purchase_orders_by_month(conn: Connection, months: int) -> list[dict[str, Any]]:
    """Orders placed per calendar month and currency, for the current month and the months before it."""
    return fetch_all(
        conn,
        """
        SELECT CAST(date_trunc('month', po.placed_on) AS timestamp) AS month, po.currency,
               count(*) AS purchase_orders, sum(po.total_amount) AS total_amount
        FROM proc_purchase_orders po
        WHERE po.placed_on >= CAST(date_trunc('month', now() AT TIME ZONE 'UTC')
                                   - make_interval(months => :months - 1) AS date)
        GROUP BY 1, po.currency
        ORDER BY 1, po.currency
        """,
        months=months,
    )


def vendor_counts(conn: Connection) -> dict[str, Any]:
    row = fetch_one(
        conn,
        "SELECT count(*) AS total_vendors, count(*) FILTER (WHERE is_active) AS active_vendors"
        " FROM proc_vendors",
    )
    assert row is not None
    return row
