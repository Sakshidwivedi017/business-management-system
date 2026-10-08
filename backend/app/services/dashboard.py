"""Role-aware dashboard overview (Layer 12), composed from existing Layer 4 reads.

Each section is included only when the caller holds the permission behind it, and every
section comes from an operation that checks that permission itself:

- inventory (inventory:read): item, location and low-stock counts
- procurement (procurement:read): vendor counts and purchase orders by status and currency
- analytics (analytics:read, owner): the owner summaries' breakdowns

The counts reuse the queries behind the owner summaries; the non-owner overviews return
aggregates of records those roles can already read through Layer 4 (low-stock items,
locations, vendors, purchase orders), and nothing from the owner-only breakdowns.
"""

from typing import Any

from sqlalchemy import Connection

from app.auth.permissions import AuthenticatedUser, Permission, ensure_permission, has_permission
from app.db.repositories import analytics as repo
from app.services import analytics
from app.services.errors import database_errors
from app.services.inventory import LOW_STOCK_FALLBACK_THRESHOLD
from app.services.procurement import OPEN_PO_STATUSES

TOP_VENDORS = 5
_INVENTORY_KEYS = ("total_items", "active_items", "active_locations", "low_stock")
_PROCUREMENT_KEYS = ("total_vendors", "active_vendors", "purchase_orders_by_status", "open_purchase_orders")


@database_errors("inventory_overview")
def inventory_overview(conn: Connection, user: AuthenticatedUser) -> dict[str, Any]:
    """Item and location counts and the low-stock count, as in inventory_summary."""
    ensure_permission(user, Permission.INVENTORY_READ)
    return {
        **repo.item_counts(conn),
        "low_stock": {
            "count": repo.low_stock_count(conn, float(LOW_STOCK_FALLBACK_THRESHOLD)),
            "fallback_threshold": LOW_STOCK_FALLBACK_THRESHOLD,
        },
    }


@database_errors("procurement_overview")
def procurement_overview(conn: Connection, user: AuthenticatedUser) -> dict[str, Any]:
    """Vendor counts and purchase orders by status and open, per currency, as in procurement_summary."""
    ensure_permission(user, Permission.PROCUREMENT_READ)
    return {
        **repo.vendor_counts(conn),
        "purchase_orders_by_status": repo.purchase_orders_by_status(conn),
        "open_purchase_orders": repo.open_purchase_orders(conn, list(OPEN_PO_STATUSES)),
    }


def dashboard_overview(conn: Connection, user: AuthenticatedUser) -> dict[str, Any]:
    """The sections this user may see; a section the user may not see is None."""
    if has_permission(user, Permission.ANALYTICS_READ):
        # The owner summaries already hold the overview counts, so they are not queried twice.
        inventory = analytics.inventory_summary(conn, user)
        procurement = analytics.procurement_summary(conn, user, top_vendors=TOP_VENDORS)
        return {
            "inventory": _pick(inventory, _INVENTORY_KEYS) if has_permission(user, Permission.INVENTORY_READ) else None,
            "procurement": (
                _pick(procurement, _PROCUREMENT_KEYS) if has_permission(user, Permission.PROCUREMENT_READ) else None
            ),
            "analytics": {
                "stock_by_location": inventory["stock_by_location"],
                "recent_transactions": inventory["recent_transactions"],
                "top_vendors_by_spend": procurement["top_vendors_by_spend"],
            },
        }
    return {
        "inventory": inventory_overview(conn, user) if has_permission(user, Permission.INVENTORY_READ) else None,
        "procurement": (
            procurement_overview(conn, user) if has_permission(user, Permission.PROCUREMENT_READ) else None
        ),
        "analytics": None,
    }


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source[key] for key in keys}
