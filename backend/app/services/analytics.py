"""Owner-only business summaries, computed deterministically in SQL."""

from datetime import date
from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission
from app.db.repositories import analytics as repo
from app.services.errors import ValidationError, database_errors
from app.services.inventory import LOW_STOCK_FALLBACK_THRESHOLD
from app.services.procurement import OPEN_PO_STATUSES

MAX_TREND_MONTHS = 36
MAX_HISTORY_MONTHS = 120  # how far back "all recorded history" reaches


@database_errors("inventory_summary")
def inventory_summary(conn: Connection, user: AuthenticatedUser, recent_days: int = 30) -> dict[str, Any]:
    ensure_permission(user, Permission.ANALYTICS_READ)
    if isinstance(recent_days, bool) or not isinstance(recent_days, int) or not 1 <= recent_days <= 365:
        raise ValidationError("recent_days must be an integer between 1 and 365")
    return {
        **repo.item_counts(conn),
        "low_stock": {
            "count": repo.low_stock_count(conn, float(LOW_STOCK_FALLBACK_THRESHOLD)),
            "fallback_threshold": LOW_STOCK_FALLBACK_THRESHOLD,
        },
        "stock_by_location": repo.stock_by_location(conn),
        "stock_by_category": repo.stock_by_category(conn),
        "recent_transactions": {
            "days": recent_days,
            "by_type": repo.recent_transaction_counts(conn, recent_days),
        },
    }


@database_errors("procurement_summary")
def procurement_summary(
    conn: Connection, user: AuthenticatedUser, top_vendors: int = 10, months: int | None = 0
) -> dict[str, Any]:
    """PO counts/values by status, open POs, top vendors and orders per month (see purchase_order_trend).
    Money is grouped by currency, never mixed."""
    ensure_permission(user, Permission.ANALYTICS_READ)
    if isinstance(top_vendors, bool) or not isinstance(top_vendors, int) or not 1 <= top_vendors <= 50:
        raise ValidationError("top_vendors must be an integer between 1 and 50")
    validate_months(months)
    summary = {
        **repo.vendor_counts(conn),
        "purchase_orders_by_status": repo.purchase_orders_by_status(conn),
        "open_purchase_orders": repo.open_purchase_orders(conn, list(OPEN_PO_STATUSES)),
        "top_vendors_by_spend": repo.top_vendors_by_spend(conn, top_vendors),
    }
    if months != 0:
        rows, history = purchase_order_trend(conn, months, values=True)
        summary |= {"purchase_orders_by_month": rows, "purchase_order_history": history}
    return summary


def validate_months(months: Any) -> None:
    """0 leaves the monthly breakdown out (the dashboard), None asks for all recorded history."""
    if months is not None and (isinstance(months, bool) or not isinstance(months, int)
                               or not 0 <= months <= MAX_TREND_MONTHS):
        raise ValidationError(f"months must be an integer between 0 and {MAX_TREND_MONTHS}, or null for all history")


def _month(day: date, back: int = 0) -> date:
    """The first day of `day`'s month, `back` months earlier."""
    index = day.year * 12 + day.month - 1 - back
    return date(index // 12, index % 12 + 1, 1)


def purchase_order_trend(conn: Connection, months: int | None, values: bool) -> tuple[list[dict], dict[str, Any]]:
    """Orders placed (placed_on) per calendar month, oldest first, and what the rows cover.

    `months` is the last N months, this one included, or None for all recorded history (at most
    MAX_HISTORY_MONTHS). The rows run from the later of the window's start and the first order's month
    to this month, with 0 for a month without orders; no month before the first order is reported.
    `values` adds order values per currency (amounts in different currencies are never added up);
    otherwise the rows hold order counts only.

    No permission check of its own: called only by operations that have checked theirs.
    """
    dates = repo.purchase_order_dates(conn)
    first_order, last_order, undated, today = (
        dates["first_order_on"], dates["last_order_on"], dates["orders_without_date"], dates["today"]
    )
    requested = _month(today, (months or MAX_HISTORY_MONTHS) - 1)
    history: dict[str, Any] = {
        "window": (f"last {months} months, this month included" if months and months > 1 else
                   "this month" if months else "all recorded history"),
        "requested_from_month": requested if months else None,
        "first_order_on": first_order,
        "last_order_on": last_order,
        "orders_without_date": undated,  # never in a month
        "through": today,  # this month is still in progress
    }
    if first_order is None:
        return [], {**history, "from_month": None, "to_month": None, "complete_history": undated == 0}
    first, last = max(requested, _month(first_order)), max(_month(today), _month(last_order))
    rows = (repo.purchase_orders_by_month if values else repo.purchase_order_counts_by_month)(conn, first, last)
    return rows, {**history, "from_month": first, "to_month": last,
                  "complete_history": first <= first_order and undated == 0}
