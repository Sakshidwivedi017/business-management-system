"""Agent tools: explicit registries of Layer 4 operations and the executor that runs them.

The model only ever proposes a tool name and arguments. The executor validates
both, supplies the authenticated user itself, and calls the Layer 4 operation,
which re-checks the permission. Results and errors come back as JSON strings
that are safe to show to the model and the user.

Read tools (TOOLS) run in a read-only transaction. Mutation tools (MUTATION_TOOLS)
never execute here: the executor hands them to Layer 8 (app.agent.mutations), which
only proposes the change until the user confirms it.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as ArgumentsError
from sqlalchemy.exc import SQLAlchemyError

from app.agent import mutations
from app.auth.permissions import AuthenticatedUser, Permission, PermissionDenied, has_permission
from app.db.connection import get_engine
from app.observability import traced_step
from app.rag import search as knowledge
from app.rag.documents import SOURCES as KNOWLEDGE_SOURCES
from app.services import BusinessError, analytics, dashboard, inventory, procurement

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 300

Text = Annotated[str, Field(min_length=1, max_length=100)]


class _Args(BaseModel):
    # Unknown fields (user_id, role, sql, ...) are rejected, and values are not coerced.
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchItemsArgs(_Args):
    query: Text = Field(description="Part of an item name or item code")
    limit: int = Field(20, ge=1, le=50)


class GetItemDetailsArgs(_Args):
    item: Text = Field(description="Item code (e.g. BO-MC-0100) or item id")


class ListLocationsArgs(_Args):
    active_only: bool = True


class GetStockByLocationArgs(_Args):
    location: Text = Field(description="Location name or id, as returned by list_locations")
    limit: int = Field(50, ge=1, le=50)


class GetTransactionHistoryArgs(_Args):
    item: Text | None = Field(None, description="Item code or id")
    location: Text | None = Field(None, description="Location name or id")
    limit: int = Field(20, ge=1, le=50)


class GetLowStockItemsArgs(_Args):
    query: Text | None = Field(None, description="Only items whose name or code contains this text")
    location: Text | None = Field(None, description="Location name or id")
    threshold: float | None = Field(
        None, ge=0, description="Quantity at or below which stock counts as low where no minimum level is set"
    )
    limit: int = Field(50, ge=1, le=50)


class ListVendorsArgs(_Args):
    name: Text | None = Field(None, description="Part of the vendor name")
    active_only: bool = False
    limit: int = Field(50, ge=1, le=50)


class GetVendorArgs(_Args):
    vendor_id: Text


class ListPurchaseOrdersArgs(_Args):
    status: Literal[procurement.PO_STATUSES] | None = None
    vendor_id: Text | None = Field(None, description="Vendor id, as returned by list_vendors")
    limit: int = Field(20, ge=1, le=50)


class GetPurchaseOrderArgs(_Args):
    purchase_order: Text = Field(description="PO number (e.g. PO-2026-0001) or PO id")


class InventorySummaryArgs(_Args):
    recent_days: int = Field(30, ge=1, le=365)


class ProcurementSummaryArgs(_Args):
    top_vendors: int = Field(10, ge=1, le=50)
    months: int = Field(12, ge=0, le=analytics.MAX_TREND_MONTHS,
                        description="Months of orders-per-month history to include, this month included")


class ProcurementOverviewArgs(_Args):
    pass


class SearchKnowledgeArgs(_Args):
    query: str = Field(min_length=1, max_length=knowledge.MAX_QUERY_LENGTH, description="What to look for, in words")
    top_k: int = Field(knowledge.DEFAULT_TOP_K, ge=1, le=knowledge.MAX_TOP_K)
    source: Literal[KNOWLEDGE_SOURCES] | None = Field(
        None, description="item: the item catalogue; item_classification: the classification guide"
    )


Quantity = Annotated[float, Field(gt=0, le=1_000_000_000)]
Note = Annotated[str, Field(min_length=1, max_length=500)]


class RecordStockMovementArgs(_Args):
    item: Text = Field(description="Item code (e.g. BO-MC-0100) or item id")
    location: Text = Field(description="Location name or id, as returned by list_locations")
    direction: Literal["inbound", "outbound"] = Field(description="inbound adds stock, outbound removes it")
    quantity: Quantity = Field(description="Amount in the item's unit, at most 3 decimal places")
    notes: Note | None = None


class PurchaseOrderLineArgs(_Args):
    item: Text = Field(description="Item code or item id")
    quantity: Quantity = Field(description="Quantity in the item's unit, at most 3 decimal places")
    unit_price: float = Field(ge=0, le=999_999_999_999.99, description="Price per unit, at most 2 decimal places")
    tax_percentage: float = Field(ge=0, le=100, description="Tax rate in percent, e.g. 18")
    notes: Note | None = None


class CreatePurchaseOrderArgs(_Args):
    vendor_id: Text = Field(description="Vendor id, as returned by list_vendors")
    lines: list[PurchaseOrderLineArgs] = Field(min_length=1, max_length=procurement.MAX_PO_LINES)
    delivery_location: Text | None = Field(None, description="Location name or id to deliver to")
    payment_terms: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "INR"


class PurchaseReceiptLineArgs(_Args):
    purchase_order: Text = Field(description="PO number (e.g. PO-2026-0001) or PO id")
    item: Text = Field(description="Item code (or item id) of the PO line that was received")
    quantity: Quantity = Field(description="Quantity received, in the PO line's unit, at most 3 decimal places")
    line_position: int | None = Field(
        None, ge=0, le=procurement.MAX_LINE_POSITION,
        description="The line's position (# in get_purchase_order); only needed when the item is on more than "
                    "one line of that PO",
    )


class RecordPurchaseReceiptArgs(_Args):
    location: Text = Field(description="Location name or id where the goods arrived, as returned by list_locations")
    lines: list[PurchaseReceiptLineArgs] = Field(min_length=1, max_length=procurement.MAX_RECEIPT_LINES)
    notes: Note | None = Field(None, description="e.g. the supplier's invoice or delivery note number")


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    args_schema: type[_Args]
    permission: Permission
    operation: Callable[..., Any]
    mutation: bool = False

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_schema.model_json_schema(),
            },
        }


_INV = Permission.INVENTORY_READ
_PROC = Permission.PROCUREMENT_READ
_ANALYTICS = Permission.ANALYTICS_READ

TOOLS: dict[str, AgentTool] = {
    tool.name: tool
    for tool in (
        AgentTool("search_items", "Find items by part of their name or code.", SearchItemsArgs, _INV,
                  inventory.search_items),
        AgentTool("get_item_details", "Item details with current stock at every location.", GetItemDetailsArgs,
                  _INV, inventory.get_item_details),
        AgentTool("list_locations", "List stock locations (warehouses). Use it to resolve a location the user "
                  "mentions.", ListLocationsArgs, _INV, inventory.list_locations),
        AgentTool("get_stock_by_location", "Current stock of items at one location.", GetStockByLocationArgs, _INV,
                  inventory.get_stock_by_location),
        AgentTool("get_transaction_history", "Recent stock transactions, newest first, optionally for one item "
                  "and/or location.", GetTransactionHistoryArgs, _INV, inventory.get_transaction_history),
        AgentTool("get_low_stock_items", "Stock rows at or below their low-stock threshold, optionally filtered "
                  "by item text and location. The result states the threshold that was applied.",
                  GetLowStockItemsArgs, _INV, inventory.get_low_stock_items),
        AgentTool("list_vendors", "List vendors, optionally filtered by part of the name.", ListVendorsArgs, _PROC,
                  procurement.list_vendors),
        AgentTool("get_vendor", "One vendor by id.", GetVendorArgs, _PROC, procurement.get_vendor),
        AgentTool("list_purchase_orders", "List purchase orders, newest first, optionally by status or vendor.",
                  ListPurchaseOrdersArgs, _PROC, procurement.list_purchase_orders),
        AgentTool("get_purchase_order", "One purchase order with its lines, receipts and payment tranches.",
                  GetPurchaseOrderArgs, _PROC, procurement.get_purchase_order),
        AgentTool("inventory_summary", "Business-wide inventory summary: item counts, low-stock count, stock by "
                  "location and category, recent transaction counts.", InventorySummaryArgs, _ANALYTICS,
                  analytics.inventory_summary),
        AgentTool("procurement_summary", "Business-wide procurement summary: purchase orders by status, open "
                  "orders, top vendors by spend, and orders placed per month (count and value per currency).",
                  ProcurementSummaryArgs, _ANALYTICS, analytics.procurement_summary),
        AgentTool("procurement_overview", "Exact purchase order counts and values by status and of open orders "
                  "(per currency), and vendor counts. Use it for questions about order statuses or how many "
                  "orders there are, instead of counting a list.", ProcurementOverviewArgs, _PROC,
                  dashboard.procurement_overview),
        AgentTool("search_knowledge", "Semantic search over descriptive catalogue text: items (name, code, "
                  "category, unit, HSN, notes) and the item classification guide (descriptions, standards, naming "
                  "conventions, examples). Use it when the user describes something in their own words, or asks "
                  "what a type of item is or how it should be named. It holds no stock, prices, orders or vendors; "
                  "use the other tools for those. Only results above a relevance threshold are returned.",
                  SearchKnowledgeArgs, _INV, knowledge.search_knowledge),
    )
}


# Layer 8: tools whose call only proposes a change; mutations.py executes it after the user confirms.
_PROPOSE = (
    " Calling it changes nothing: the system shows the user what would happen and asks them to confirm. "
    "Ask for any missing details first, and never say the change was made."
)
MUTATION_TOOLS: dict[str, AgentTool] = {
    tool.name: tool
    for tool in (
        AgentTool("record_stock_movement", "Propose receiving (inbound) or removing (outbound) a quantity of one item "
                  "at one location." + _PROPOSE, RecordStockMovementArgs, Permission.INVENTORY_WRITE,
                  inventory.record_stock_movement, mutation=True),
        AgentTool("create_purchase_order", "Propose a new purchase order to an active vendor, with item, quantity, "
                  "unit price and tax rate per line; totals and the PO number are computed by the system."
                  + _PROPOSE, CreatePurchaseOrderArgs, Permission.PROCUREMENT_WRITE,
                  procurement.create_purchase_order, mutation=True),
        AgentTool("record_purchase_receipt", "Propose recording goods received against existing purchase orders: "
                  "one location, and per PO line the PO number, item code and quantity received in the line's "
                  "unit. Several POs can go in one proposal. Use get_purchase_order to see a PO's lines and what "
                  "remains to be received, and list_locations to resolve the location. Prices come from the PO."
                  + _PROPOSE, RecordPurchaseReceiptArgs, Permission.PROCUREMENT_WRITE,
                  procurement.record_purchase_receipt, mutation=True),
    )
}


def available_tools(user: AuthenticatedUser) -> list[AgentTool]:
    """Tools offered to the model for this user: permitted reads, plus permitted Layer 8 mutation proposals."""
    reads = [tool for tool in TOOLS.values() if not tool.mutation and has_permission(user, tool.permission)]
    return reads + [tool for tool in MUTATION_TOOLS.values() if _is_guarded(tool) and has_permission(user, tool.permission)]


def _is_guarded(tool: AgentTool) -> bool:
    """A mutation tool is usable only with Layer 8 behind it, wired to the same Layer 4 operation."""
    guard = mutations.MUTATIONS.get(tool.name)
    return tool.mutation and guard is not None and guard.operation is tool.operation


@dataclass(frozen=True)
class ToolResult:
    content: str  # JSON, safe for the model and the user
    is_error: bool
    mutation: mutations.MutationStatus | None = None  # set when a change was proposed
    error_kind: str | None = None  # coarse failure class for tracing, e.g. "invalid_arguments"


def execute_tool(user: AuthenticatedUser, name: str, args: Any, *, thread_id: str | None = None) -> ToolResult:
    """Run a read tool, or propose a mutation (Layer 8) within the conversation `thread_id`."""
    # Layer 16: a span named after the tool, with only its outcome (never arguments or results).
    # A name the registries do not know is model text, so it is not used as the span name.
    known = isinstance(name, str) and (name in TOOLS or name in MUTATION_TOOLS)
    with traced_step(name if known else "unknown_tool", "tool", tool=name if known else None,
                     mutation=name in MUTATION_TOOLS if known else None) as span:
        result = _execute(user, name, args, thread_id)
        span["outcome"] = "error" if result.is_error else "proposed" if result.mutation else "success"
        span["error_kind"] = result.error_kind
    return result


def _execute(user: AuthenticatedUser, name: str, args: Any, thread_id: str | None) -> ToolResult:
    mutation = MUTATION_TOOLS.get(name) if isinstance(name, str) else None
    if mutation is not None and _is_guarded(mutation):
        return _propose(user, mutation, args, thread_id)
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None or tool.mutation:
        logger.warning("Agent requested unknown tool")
        return _error(f"Unknown tool '{str(name)[:64]}'", "unknown_tool")
    parsed = _authorize(user, tool, args)
    if isinstance(parsed, ToolResult):
        return parsed

    def read():
        with get_engine().connect() as conn:
            # Read-only transaction: even a wrongly registered write could not change data.
            conn.execution_options(postgresql_readonly=True)
            return tool.operation(conn, user, **parsed.model_dump())

    outcome = _guarded(user, name, read)
    if isinstance(outcome, ToolResult):
        return outcome
    logger.info("Tool %s succeeded", name)
    # default=str keeps Decimal exact ("102.50") and renders dates and UUIDs readably.
    return ToolResult(json.dumps({"result": outcome}, default=str, ensure_ascii=False), is_error=False)


def _propose(user: AuthenticatedUser, tool: AgentTool, args: Any, thread_id: str | None) -> ToolResult:
    parsed = _authorize(user, tool, args)
    if isinstance(parsed, ToolResult):
        return parsed
    if thread_id is None:
        return _error("Changes can only be proposed within a conversation", "no_conversation")
    status = _guarded(user, tool.name, lambda: mutations.propose(user, thread_id, tool.name, parsed.model_dump()))
    if isinstance(status, ToolResult):
        return status
    return ToolResult(json.dumps({"result": status.model_dump(mode="json")}, ensure_ascii=False), is_error=False,
                      mutation=status)


def _authorize(user: AuthenticatedUser, tool: AgentTool, args: Any) -> _Args | ToolResult:
    """Strict arguments, then the authenticated user's permission; both before any database access."""
    try:
        parsed = tool.args_schema.model_validate(args)
    except ArgumentsError as exc:
        logger.info("Tool %s rejected: invalid arguments", tool.name)
        return _error(f"Invalid arguments for {tool.name}: {_describe(exc)}", "invalid_arguments")
    if not has_permission(user, tool.permission):
        logger.info("Tool %s denied for role %s", tool.name, user.role)
        return _error(f"You do not have permission to use {tool.name}", "permission_denied")
    return parsed


def _guarded(user: AuthenticatedUser, name: str, call: Callable[[], Any]) -> Any:
    """Run a tool body; every failure becomes a safe error ToolResult."""
    try:
        return call()
    except PermissionDenied:
        # Layer 4 is the final authority; reaching this means the registry and RBAC disagree.
        logger.warning("Tool %s denied by Layer 4 for role %s", name, user.role)
        return _error(f"You do not have permission to use {name}", "permission_denied")
    except BusinessError as exc:
        logger.info("Tool %s failed: %s", name, type(exc).__name__)
        return _error(str(exc), "business_error")
    except SQLAlchemyError as exc:
        logger.error("Tool %s database failure: %s", name, type(exc).__name__)
        return _error("Business data is temporarily unavailable; please try again later", "database_error")
    except Exception:
        logger.exception("Tool %s failed unexpectedly", name)
        return _error("The tool failed unexpectedly", "unexpected_error")


def _error(message: str, kind: str) -> ToolResult:
    return ToolResult(json.dumps({"error": message[:MAX_ERROR_LENGTH]}), is_error=True, error_kind=kind)


def _describe(exc: ArgumentsError) -> str:
    # Field paths and messages only; the offending input values are never echoed.
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}" for error in exc.errors()[:5]
    )
