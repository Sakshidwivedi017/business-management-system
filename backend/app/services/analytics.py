"""Owner-only business summaries, computed deterministically in SQL."""

from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission
from app.db.repositories import analytics as repo
from app.services.errors import ValidationError, database_errors
from app.services.inventory import LOW_STOCK_FALLBACK_THRESHOLD
from app.services.procurement import OPEN_PO_STATUSES

MAX_TREND_MONTHS = 36


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
    conn: Connection, user: AuthenticatedUser, top_vendors: int = 10, months: int = 0
) -> dict[str, Any]:
    """PO counts/values by status, open POs, top vendors and, if `months` > 0, orders per month for that many
    months (this one included). Money is grouped by currency, never mixed."""
    ensure_permission(user, Permission.ANALYTICS_READ)
    if isinstance(top_vendors, bool) or not isinstance(top_vendors, int) or not 1 <= top_vendors <= 50:
        raise ValidationError("top_vendors must be an integer between 1 and 50")
    if isinstance(months, bool) or not isinstance(months, int) or not 0 <= months <= MAX_TREND_MONTHS:
        raise ValidationError(f"months must be an integer between 0 and {MAX_TREND_MONTHS}")
    summary = {
        **repo.vendor_counts(conn),
        "purchase_orders_by_status": repo.purchase_orders_by_status(conn),
        "open_purchase_orders": repo.open_purchase_orders(conn, list(OPEN_PO_STATUSES)),
        "top_vendors_by_spend": repo.top_vendors_by_spend(conn, top_vendors),
    }
    if months:
        summary["purchase_orders_by_month"] = repo.purchase_orders_by_month(conn, months)
    return summary
