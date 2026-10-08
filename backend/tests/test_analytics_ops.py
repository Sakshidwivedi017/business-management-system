"""Owner analytics against the live database, cross-checked with independent SQL."""

import pytest
from sqlalchemy import text

from app.auth.permissions import PermissionDenied
from app.services import ValidationError
from app.services import analytics as ops
from app.services import inventory as inv_ops
from app.services import procurement as proc_ops


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


@pytest.mark.parametrize("role", ["inventory_manager", "procurement_manager"])
def test_non_owners_denied(conn, demo_users, role):
    with pytest.raises(PermissionDenied):
        ops.inventory_summary(conn, demo_users[role])
    with pytest.raises(PermissionDenied):
        ops.procurement_summary(conn, demo_users[role])


def test_inventory_summary(conn, demo_users):
    summary = ops.inventory_summary(conn, demo_users["owner"])

    assert summary["total_items"] == scalar(conn, "SELECT count(*) FROM inv_items")
    assert summary["active_items"] == scalar(conn, "SELECT count(*) FROM inv_items WHERE status = 'active'")
    assert summary["active_locations"] == scalar(conn, "SELECT count(*) FROM inv_locations WHERE is_active")
    assert summary["low_stock"]["count"] == len(
        inv_ops.get_low_stock_items(conn, demo_users["owner"], limit=200)["items"]
    )
    assert sum(c["active_items"] for c in summary["stock_by_category"]) == summary["active_items"]
    for loc in summary["stock_by_location"]:
        assert loc["items_in_stock"] + loc["items_out_of_stock"] == loc["stocked_items"]
    assert sum(loc["stocked_items"] for loc in summary["stock_by_location"]) == scalar(
        conn,
        "SELECT count(*) FROM inv_current_stock s JOIN inv_items i ON i.id = s.item_id"
        " JOIN inv_locations l ON l.id = s.location_id WHERE i.status = 'active' AND l.is_active",
    )
    assert summary["recent_transactions"]["days"] == 30


def test_procurement_summary(conn, demo_users):
    summary = ops.procurement_summary(conn, demo_users["owner"])

    assert summary["total_vendors"] == scalar(conn, "SELECT count(*) FROM proc_vendors")
    by_status = summary["purchase_orders_by_status"]
    assert sum(r["purchase_orders"] for r in by_status) == scalar(conn, "SELECT count(*) FROM proc_purchase_orders")
    for row in by_status:
        assert row["total_amount"] == scalar(
            conn,
            "SELECT sum(total_amount) FROM proc_purchase_orders WHERE status = :s AND currency = :c",
            s=row["status"], c=row["currency"],
        )
    assert sum(r["purchase_orders"] for r in summary["open_purchase_orders"]) == scalar(
        conn, "SELECT count(*) FROM proc_purchase_orders WHERE status IN ('placed', 'partial')"
    )
    spends = [v["total_amount"] for v in summary["top_vendors_by_spend"]]
    assert spends == sorted(spends, reverse=True) and len(spends) <= 10


def test_summary_reflects_new_purchase_order(conn, demo_users):
    owner, pm = demo_users["owner"], demo_users["procurement_manager"]

    def placed_inr(summary):
        row = next((r for r in summary["purchase_orders_by_status"] if (r["status"], r["currency"]) == ("placed", "INR")), None)
        return (row["purchase_orders"], row["total_amount"]) if row else (0, 0)

    count_before, total_before = placed_inr(ops.procurement_summary(conn, owner))
    vendor = scalar(conn, "SELECT id FROM proc_vendors WHERE is_active LIMIT 1")
    item = scalar(conn, "SELECT id FROM inv_items WHERE status = 'active' LIMIT 1")
    created = proc_ops.create_purchase_order(
        conn, pm, vendor_id=vendor, lines=[{"item": item, "quantity": 2, "unit_price": "50.25", "tax_percentage": 18}]
    )
    count_after, total_after = placed_inr(ops.procurement_summary(conn, owner))
    assert count_after == count_before + 1
    assert total_after == total_before + created["purchase_order"]["total_amount"]


@pytest.mark.parametrize("bad", [0, 366, "30", True])
def test_summary_parameters_validated(conn, demo_users, bad):
    with pytest.raises(ValidationError):
        ops.inventory_summary(conn, demo_users["owner"], recent_days=bad)


def test_purchase_orders_by_month_match_the_orders(conn, demo_users):
    owner = demo_users["owner"]
    assert "purchase_orders_by_month" not in ops.procurement_summary(conn, owner)  # the dashboard's call
    months = ops.procurement_summary(conn, owner, months=12)["purchase_orders_by_month"]
    expected = conn.execute(text(
        "SELECT count(*), COALESCE(sum(total_amount), 0) FROM proc_purchase_orders WHERE placed_on >= "
        "CAST(date_trunc('month', now() AT TIME ZONE 'UTC') - interval '11 months' AS date)"
    )).one()
    assert sum(row["purchase_orders"] for row in months) == expected[0]
    assert sum(row["total_amount"] for row in months) == expected[1]
    assert [r["month"] for r in months] == sorted(r["month"] for r in months)
    assert all(r["month"].day == 1 and r["month"].hour == 0 for r in months)


@pytest.mark.parametrize("months", [-1, 37, True, "12"])
def test_month_count_validated(conn, demo_users, months):
    with pytest.raises(ValidationError):
        ops.procurement_summary(conn, demo_users["owner"], months=months)
