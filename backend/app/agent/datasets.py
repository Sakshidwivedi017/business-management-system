"""Rows behind a response plan, made safe for the browser (Layer 14).

The Layer 7 plan points at rows inside this turn's tool results (Dataset: tool, tool_call_id,
path). This module copies those rows out of the same tool messages the agent used to write
its answer, so nothing is fetched or executed again, and the data is exactly what the
authenticated user's own tool calls returned (Layer 4 already checked permissions).

Serialization is deliberate, never a pass-through: each dataset has an explicit column
allowlist, so ids, user references, notes blobs and nested structures never leave the server.
Only scalar values are kept, long text is shortened, and at most MAX_ROWS rows of at most
MAX_DATASETS datasets are sent; `total_rows` and `truncated` say when more existed.
"""

import json
import math
from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.messages import AnyMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from app.agent.planning import ChartKind, DataKind, Presentation, ResponsePlan

MAX_ROWS = 50  # rows per dataset sent to the browser
MAX_DATASETS = 6  # datasets per reply
MAX_TEXT_LENGTH = 200  # characters per text cell

ColumnKind = Literal["text", "number", "amount", "date", "boolean"]
Cell = str | int | float | bool | None


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class DatasetColumn(_Model):
    key: str
    label: str
    # "amount" values are money in the row's `currency` column; they are never combined across currencies.
    kind: ColumnKind


class DatasetPayload(_Model):
    """One planned dataset with its rows. Mirrors Dataset (tool, kind, x, y, series, chart)."""

    tool: str
    title: str
    kind: DataKind
    x: str | None = None
    y: tuple[str, ...] = ()
    series: str | None = None
    chart: ChartKind | None = None
    columns: tuple[DatasetColumn, ...]
    rows: tuple[dict[str, Cell], ...]
    total_rows: int  # rows in the tool result
    truncated: bool  # True when fewer rows are sent than the tool result holds


class Metric(_Model):
    """A headline figure from a summary tool, as the backend computed it."""

    label: str
    value: int | float | str


class ChatData(_Model):
    datasets: tuple[DatasetPayload, ...] = ()
    metrics: tuple[Metric, ...] = ()


# --- serialization rules -----------------------------------------------------------------------

_T, _N, _A, _D, _B = "text", "number", "amount", "date", "boolean"


def _cols(*columns: tuple[str, str, ColumnKind]) -> tuple[DatasetColumn, ...]:
    return tuple(DatasetColumn(key=key, label=label, kind=kind) for key, label, kind in columns)


_CODE, _ITEM, _UNIT, _QTY = ("item_code", "Code", _T), ("item_name", "Item", _T), ("unit", "Unit", _T), ("quantity", "Quantity", _N)
_LOCATION = ("location_name", "Location", _T)
_CURRENCY = ("currency", "Currency", _T)

# Keyed like planning.RESULT_SHAPES: (tool, path) -> (title, columns shown, in order).
COLUMNS: dict[tuple[str, str], tuple[str, tuple[DatasetColumn, ...]]] = {
    ("search_items", ""): ("Items", _cols(
        ("item_code", "Code", _T), ("name", "Item", _T), ("category_name", "Category", _T),
        ("sub_category_name", "Sub-category", _T), _UNIT, ("status", "Status", _T))),
    ("get_item_details", "stock"): ("Stock by location", _cols(
        _LOCATION, _QTY, _UNIT, ("min_stock_level", "Minimum", _N), ("last_transaction_at", "Last movement", _D))),
    ("list_locations", ""): ("Locations", _cols(
        ("name", "Location", _T), ("address", "Address", _T), ("is_active", "Active", _B))),
    ("get_stock_by_location", "stock"): ("Stock at location", _cols(
        _CODE, _ITEM, _QTY, _UNIT, ("min_stock_level", "Minimum", _N), ("last_transaction_at", "Last movement", _D))),
    ("get_transaction_history", ""): ("Stock transactions", _cols(
        ("created_at", "Date", _D), _CODE, _ITEM, _LOCATION, ("transaction_type", "Type", _T), _QTY,
        ("unit_cost", "Unit cost", _A), _CURRENCY)),
    ("get_low_stock_items", "items"): ("Low-stock items", _cols(
        _CODE, _ITEM, _LOCATION, _QTY, _UNIT, ("effective_threshold", "Threshold", _N))),
    ("list_vendors", ""): ("Vendors", _cols(
        ("name", "Vendor", _T), ("code", "Code", _T), ("city", "City", _T), ("payment_terms", "Payment terms", _T),
        ("is_active", "Active", _B))),
    ("list_purchase_orders", ""): ("Purchase orders", _cols(
        ("po_number", "PO number", _T), ("status", "Status", _T), ("placed_on", "Placed on", _D),
        ("vendor_name", "Vendor", _T), _CURRENCY, ("total_amount", "Total", _A),
        ("expected_delivery_date", "Expected delivery", _D))),
    # PO lines carry no currency of their own, so their prices are plain numbers here.
    ("get_purchase_order", "lines"): ("Order lines", _cols(
        ("position", "#", _N), _CODE, _ITEM, ("quantity_ordered", "Ordered", _N),
        ("quantity_received", "Received", _N), _UNIT, ("unit_price", "Unit price", _N),
        ("tax_percentage", "Tax %", _N), ("line_total", "Line total", _N))),
    ("inventory_summary", "stock_by_location"): ("Stock by location", _cols(
        _LOCATION, ("stocked_items", "Stocked items", _N), ("items_in_stock", "In stock", _N),
        ("items_out_of_stock", "Out of stock", _N))),
    ("inventory_summary", "stock_by_category"): ("Stock by category", _cols(
        ("category_name", "Category", _T), ("active_items", "Active items", _N), ("items_in_stock", "In stock", _N))),
    ("inventory_summary", "recent_transactions.by_type"): ("Recent transactions by type", _cols(
        ("transaction_type", "Type", _T), ("transactions", "Transactions", _N))),
    ("procurement_summary", "purchase_orders_by_status"): ("Purchase orders by status", _cols(
        ("status", "Status", _T), _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Value", _A))),
    ("procurement_summary", "open_purchase_orders"): ("Open purchase orders", _cols(
        _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Value", _A),
        ("lines_pending_delivery", "Lines pending", _N))),
    ("procurement_summary", "top_vendors_by_spend"): ("Top vendors by spend", _cols(
        ("vendor_name", "Vendor", _T), _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Spend", _A))),
    ("procurement_summary", "purchase_orders_by_month"): ("Purchase orders by month", _cols(
        ("month", "Month", _D), _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Value", _A))),
    ("procurement_overview", "purchase_orders_by_status"): ("Purchase orders by status", _cols(
        ("status", "Status", _T), _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Value", _A))),
    ("procurement_overview", "open_purchase_orders"): ("Open purchase orders", _cols(
        _CURRENCY, ("purchase_orders", "Orders", _N), ("total_amount", "Value", _A),
        ("lines_pending_delivery", "Lines pending", _N))),
}

# Headline figures of the summary tools: (dotted path in the result, label).
METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "inventory_summary": (
        ("total_items", "Items in catalogue"), ("active_items", "Active items"),
        ("active_locations", "Active locations"), ("low_stock.count", "Low-stock positions"),
    ),
    "procurement_summary": (("total_vendors", "Vendors"), ("active_vendors", "Active vendors")),
    "procurement_overview": (("total_vendors", "Vendors"), ("active_vendors", "Active vendors")),
}


# --- extraction ----------------------------------------------------------------------------------


def _at(value: Any, path: str) -> Any:
    for key in filter(None, path.split(".")):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _result(message: ToolMessage) -> Any:
    if message.status == "error":
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    return payload.get("result") if isinstance(payload, dict) else None


def _cell(value: Any) -> Cell:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT_LENGTH else value[: MAX_TEXT_LENGTH - 1] + "…"
    return None  # nested objects and lists are never sent


def _payload(dataset, rows: list[Any]) -> DatasetPayload | None:
    rule = COLUMNS.get((dataset.tool, dataset.path))
    if rule is None:
        return None
    title, columns = rule
    records = [row for row in rows if isinstance(row, dict)]
    return DatasetPayload(
        tool=dataset.tool, title=title, kind=dataset.kind, x=dataset.x, y=dataset.y, series=dataset.series,
        chart=dataset.chart, columns=columns,
        rows=tuple({column.key: _cell(row.get(column.key)) for column in columns} for row in records[:MAX_ROWS]),
        total_rows=len(records), truncated=len(records) > MAX_ROWS,
    )


def chat_data(messages: Sequence[AnyMessage], plan: ResponsePlan) -> ChatData:
    """The rows and figures behind `plan`, read from the tool results it was planned from.

    Only tool calls the plan itself names are read (never ids from a client), and only when the
    plan presents structured data. Unknown shapes and unreadable results are skipped.
    """
    if plan.presentation is Presentation.TEXT or not plan.datasets:
        return ChatData()
    wanted = {dataset.tool_call_id for dataset in plan.datasets}
    results = {
        m.tool_call_id: m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id in wanted
    }

    datasets: list[DatasetPayload] = []
    metrics: list[Metric] = []
    summarized: set[str] = set()
    for dataset in plan.datasets:
        message = results.get(dataset.tool_call_id)
        if message is None or message.name != dataset.tool:
            continue
        result = _result(message)
        rows = _at(result, dataset.path)
        if len(datasets) < MAX_DATASETS and isinstance(rows, list):
            payload = _payload(dataset, rows)
            if payload is not None:
                datasets.append(payload)
        if dataset.tool in METRICS and dataset.tool_call_id not in summarized:
            summarized.add(dataset.tool_call_id)
            for path, label in METRICS[dataset.tool]:
                value = _at(result, path)
                metric = Metric(label=label, value=value) if isinstance(value, int | float | str) and not \
                    isinstance(value, bool) else None
                if metric is not None and metric not in metrics:  # the same figure from two tools is shown once
                    metrics.append(metric)
    return ChatData(datasets=tuple(datasets), metrics=tuple(metrics))
