"""GET /api/dashboard: the caller's role-aware overview (Layer 12).

Identity and role come only from the access token; the endpoint takes no parameters.
Which sections are returned is decided by the backend permissions (app.services.dashboard),
and each section's operation checks its permission again. Amounts are decimals serialized
as strings and always paired with their currency: they are never summed across currencies.
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from app.auth.dependencies import get_current_user_released
from app.auth.permissions import AuthenticatedUser, PermissionDenied, Role
from app.db.connection import get_engine
from app.services import BusinessError
from app.services.dashboard import dashboard_overview

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

_UNAVAILABLE = "The dashboard is temporarily unavailable; please try again"


class _Model(BaseModel):
    # Only the declared fields are serialized: ids and any other row columns are dropped.
    model_config = ConfigDict(frozen=True, extra="ignore")


class DashboardUser(_Model):
    full_name: str
    role: Role


class LowStock(_Model):
    count: int  # stock rows (item at a location) at or below their minimum level
    fallback_threshold: Decimal  # the minimum used where a row has none


class InventoryOverview(_Model):
    total_items: int
    active_items: int
    active_locations: int
    low_stock: LowStock


class PurchaseOrdersByStatus(_Model):
    status: str
    currency: str
    purchase_orders: int
    total_amount: Decimal | None


class OpenPurchaseOrders(_Model):
    currency: str
    purchase_orders: int
    total_amount: Decimal | None
    lines_pending_delivery: int


class ProcurementOverview(_Model):
    total_vendors: int
    active_vendors: int
    purchase_orders_by_status: list[PurchaseOrdersByStatus]
    open_purchase_orders: list[OpenPurchaseOrders]


class LocationStock(_Model):
    location_name: str
    stocked_items: int
    items_in_stock: int
    items_out_of_stock: int


class TransactionTypeCount(_Model):
    transaction_type: str
    transactions: int


class RecentTransactions(_Model):
    days: int
    by_type: list[TransactionTypeCount]


class VendorSpend(_Model):
    vendor_name: str
    currency: str
    purchase_orders: int
    total_amount: Decimal | None


class BusinessAnalytics(_Model):
    stock_by_location: list[LocationStock]
    recent_transactions: RecentTransactions
    top_vendors_by_spend: list[VendorSpend]


class DashboardResponse(_Model):
    user: DashboardUser
    inventory: InventoryOverview | None  # inventory:read
    procurement: ProcurementOverview | None  # procurement:read
    analytics: BusinessAnalytics | None  # analytics:read (owner)


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(user: AuthenticatedUser = Depends(get_current_user_released)) -> DashboardResponse:
    try:
        # One short-lived, read-only connection, returned to the pool before the response is built.
        with get_engine().connect() as conn:
            sections = dashboard_overview(conn, user)
    except PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden") from None
    except (BusinessError, SQLAlchemyError) as exc:
        # Layer 4 already logged and sanitized database errors; connection failures land here too.
        logger.error("Dashboard failed for user %s: %s", user.id, type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None
    except Exception:
        logger.exception("Dashboard failed for user %s", user.id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _UNAVAILABLE) from None
    return DashboardResponse(user=DashboardUser(full_name=user.full_name, role=user.role), **sections)
