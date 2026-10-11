"""Owner analytics against the live database, cross-checked with independent SQL."""

import pytest
from sqlalchemy import text

from app.auth.permissions import PermissionDenied
from app.services import ValidationError
from app.services import analytics as ops
from app.services import dashboard as dash_ops
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
    with pytest.raises(ValidationError):
        dash_ops.procurement_overview(conn, demo_users["procurement_manager"], months=months)


# --- orders per month: coverage, windows and gaps, against the purchase orders' own dates ---------------------

# Independent of the code under test: orders per (month, currency) by placed_on, months with orders only.
_ORDERS_BY_MONTH = """
    SELECT CAST(date_trunc('month', placed_on) AS date) AS month, currency, count(*) AS n, sum(total_amount) AS amount
    FROM proc_purchase_orders WHERE placed_on IS NOT NULL GROUP BY 1, 2
"""


def _month_index(day):
    return day.year * 12 + day.month


def _sql_months(conn):
    return {(r.month, r.currency): (r.n, r.amount) for r in conn.execute(text(_ORDERS_BY_MONTH))}


def _today(conn):
    return scalar(conn, "SELECT CAST(now() AT TIME ZONE 'UTC' AS date)")


def _assert_consecutive_months(rows, first, last):
    months = sorted({r["month"].date() for r in rows})
    assert months[0] == first and months[-1] == last
    assert [_month_index(m) for m in months] == list(range(_month_index(first), _month_index(last) + 1))
    assert [r["month"] for r in rows] == sorted(r["month"] for r in rows)  # oldest first


def test_all_recorded_history_counts_every_dated_order(conn, demo_users):
    summary = ops.procurement_summary(conn, demo_users["owner"], months=None)
    rows, history = summary["purchase_orders_by_month"], summary["purchase_order_history"]
    first_order, last_order = conn.execute(text("SELECT min(placed_on), max(placed_on) FROM proc_purchase_orders")).one()
    today = _today(conn)

    assert history["window"] == "all recorded history" and history["requested_from_month"] is None
    assert (history["first_order_on"], history["last_order_on"], history["through"]) == (first_order, last_order, today)
    _assert_consecutive_months(rows, first_order.replace(day=1), max(today, last_order).replace(day=1))
    assert history["complete_history"] is (scalar(conn, "SELECT count(*) FROM proc_purchase_orders "
                                                        "WHERE placed_on IS NULL") == 0)
    # Every row agrees with the purchase orders' own dates, per currency; months without orders are 0.
    expected = _sql_months(conn)
    got = {(r["month"].date(), r["currency"]): (r["purchase_orders"], r["total_amount"]) for r in rows}
    assert {key: value for key, value in got.items() if value[0]} == expected
    assert all(amount == 0 for count, amount in got.values() if count == 0)
    assert sum(count for count, _ in got.values()) == scalar(
        conn, "SELECT count(*) FROM proc_purchase_orders WHERE placed_on IS NOT NULL")


def test_requested_window_starts_where_asked_and_never_before_the_first_order(conn, demo_users):
    today = _today(conn)
    first_order = scalar(conn, "SELECT min(placed_on) FROM proc_purchase_orders")
    for months in (1, 2, 6, 36):
        summary = ops.procurement_summary(conn, demo_users["owner"], months=months)
        rows, history = summary["purchase_orders_by_month"], summary["purchase_order_history"]
        requested = scalar(conn, "SELECT CAST(date_trunc('month', now() AT TIME ZONE 'UTC') "
                                 "- make_interval(months => :m - 1) AS date)", m=months)
        assert history["requested_from_month"] == requested
        assert history["from_month"] == max(requested, first_order.replace(day=1))
        assert all(r["month"].date() >= history["from_month"] for r in rows)
        before = scalar(conn, "SELECT count(*) FROM proc_purchase_orders WHERE placed_on < :start",
                        start=history["from_month"])
        assert history["complete_history"] is (before == 0)
        in_window = scalar(conn, "SELECT count(*) FROM proc_purchase_orders WHERE placed_on >= :start",
                           start=history["from_month"])
        assert sum(r["purchase_orders"] for r in rows) == in_window
        if rows:
            _assert_consecutive_months(rows, history["from_month"], max(today, history["last_order_on"]).replace(day=1))


def test_gap_months_are_zero_and_months_before_the_first_order_are_absent(conn, demo_users):
    # Inside the rolled-back test transaction: one order moved three months before the first recorded order.
    first_order = scalar(conn, "SELECT min(placed_on) FROM proc_purchase_orders")
    moved = scalar(conn, "SELECT id FROM proc_purchase_orders ORDER BY placed_on DESC, id LIMIT 1")
    early = scalar(conn, "SELECT CAST(:d - interval '3 months' AS date)", d=first_order)
    conn.execute(text("UPDATE proc_purchase_orders SET placed_on = :d WHERE id = :id"), {"d": early, "id": moved})

    rows = dash_ops.procurement_overview(conn, demo_users["procurement_manager"], months=None)["purchase_orders_by_month"]
    by_month = {r["month"].date(): r["purchase_orders"] for r in rows}
    assert min(by_month) == early.replace(day=1)  # nothing reported before the (new) first order
    gap = [m for m in by_month if early.replace(day=1) < m < first_order.replace(day=1)]
    assert len(gap) == 2 and all(by_month[m] == 0 for m in gap)  # recorded history with no orders: 0, not missing
    assert by_month == {m: _sql_counts(conn).get(m, 0) for m in by_month}

    # A window that starts after the moved order no longer covers all recorded history.
    window = dash_ops.procurement_overview(conn, demo_users["procurement_manager"], months=1)["purchase_order_history"]
    assert window["complete_history"] is False


def _sql_counts(conn):
    counts = {}
    for (month, _), (n, _) in _sql_months(conn).items():
        counts[month] = counts.get(month, 0) + n
    return counts


def test_orders_without_a_date_are_reported_not_placed_in_a_month(conn, demo_users):
    total = scalar(conn, "SELECT count(*) FROM proc_purchase_orders")
    conn.execute(text("UPDATE proc_purchase_orders SET placed_on = NULL WHERE id = "
                      "(SELECT id FROM proc_purchase_orders ORDER BY id LIMIT 1)"))
    summary = ops.procurement_summary(conn, demo_users["owner"], months=None)
    history = summary["purchase_order_history"]
    assert history["orders_without_date"] == 1 and history["complete_history"] is False
    assert sum(r["purchase_orders"] for r in summary["purchase_orders_by_month"]) == total - 1


def test_procurement_manager_gets_order_counts_per_month_without_values(conn, demo_users):
    pm, owner = demo_users["procurement_manager"], demo_users["owner"]
    overview = dash_ops.procurement_overview(conn, pm, months=None)
    rows = overview["purchase_orders_by_month"]
    assert rows and all(set(row) == {"month", "purchase_orders"} for row in rows)  # counts only, no money
    assert "top_vendors_by_spend" not in overview
    # The same orders as the owner's per-currency rows, counted across currencies.
    owner_rows = ops.procurement_summary(conn, owner, months=None)["purchase_orders_by_month"]
    totals = {}
    for row in owner_rows:
        totals[row["month"]] = totals.get(row["month"], 0) + row["purchase_orders"]
    assert {r["month"]: r["purchase_orders"] for r in rows} == totals
    assert overview["purchase_order_history"] == ops.procurement_summary(conn, owner, months=None)[
        "purchase_order_history"]
    # The dashboard's calls are unchanged.
    assert "purchase_orders_by_month" not in dash_ops.procurement_overview(conn, pm)
    assert "purchase_order_history" not in ops.procurement_summary(conn, owner)


def test_monthly_orders_keep_their_permissions(conn, demo_users):
    with pytest.raises(PermissionDenied):
        dash_ops.procurement_overview(conn, demo_users["inventory_manager"], months=None)
    with pytest.raises(PermissionDenied):
        ops.procurement_summary(conn, demo_users["procurement_manager"], months=None)
