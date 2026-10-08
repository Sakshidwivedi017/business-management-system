"""Every business operation enforces its permission before touching the database.

Denied calls get conn=None: if an operation reached the database first, it
would fail with AttributeError instead of PermissionDenied.
"""

import uuid

import pytest

from app.auth.permissions import ROLE_PERMISSIONS, AuthenticatedUser, Permission, PermissionDenied, Role
from app.services import analytics, inventory, procurement

OPERATIONS = [
    (inventory.search_items, Permission.INVENTORY_READ, {"query": "x"}),
    (inventory.get_item_details, Permission.INVENTORY_READ, {"item": "x"}),
    (inventory.list_locations, Permission.INVENTORY_READ, {}),
    (inventory.get_stock_by_location, Permission.INVENTORY_READ, {"location": "x"}),
    (inventory.get_transaction_history, Permission.INVENTORY_READ, {}),
    (inventory.get_low_stock_items, Permission.INVENTORY_READ, {}),
    (
        inventory.record_stock_movement,
        Permission.INVENTORY_WRITE,
        {"item": "x", "location": "x", "direction": "inbound", "quantity": 1},
    ),
    (procurement.list_vendors, Permission.PROCUREMENT_READ, {}),
    (procurement.get_vendor, Permission.PROCUREMENT_READ, {"vendor_id": "x"}),
    (procurement.list_purchase_orders, Permission.PROCUREMENT_READ, {}),
    (procurement.get_purchase_order, Permission.PROCUREMENT_READ, {"purchase_order": "x"}),
    (
        procurement.create_purchase_order,
        Permission.PROCUREMENT_WRITE,
        {"vendor_id": "x", "lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 0}]},
    ),
    (analytics.inventory_summary, Permission.ANALYTICS_READ, {}),
    (analytics.procurement_summary, Permission.ANALYTICS_READ, {}),
]

DENIED_CASES = [
    (op, permission, kwargs, role)
    for op, permission, kwargs in OPERATIONS
    for role in Role
    if permission not in ROLE_PERMISSIONS[role]
]


@pytest.mark.parametrize(
    ("op", "permission", "kwargs", "role"),
    DENIED_CASES,
    ids=[f"{op.__name__}-{role}" for op, _, _, role in DENIED_CASES],
)
def test_operation_denied_before_database_access(op, permission, kwargs, role):
    user = AuthenticatedUser(id=uuid.uuid4(), email="x@test.local", full_name="X", role=role)
    with pytest.raises(PermissionDenied):
        op(None, user, **kwargs)


def test_denied_matrix_matches_layer3_mapping():
    # 14 operations; each role is denied exactly the operations its mapping excludes.
    assert len(OPERATIONS) == 14
    denied = {role: {op.__name__ for op, _, _, r in DENIED_CASES if r is role} for role in Role}
    assert denied[Role.INVENTORY_MANAGER] == {
        "list_vendors", "get_vendor", "list_purchase_orders", "get_purchase_order",
        "create_purchase_order", "inventory_summary", "procurement_summary",
    }
    assert denied[Role.PROCUREMENT_MANAGER] == {
        "record_stock_movement", "inventory_summary", "procurement_summary",
    }
    assert denied[Role.OWNER] == {"record_stock_movement", "create_purchase_order"}
