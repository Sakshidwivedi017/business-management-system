"""Intent classification and response planning (Layer 7).

A ResponsePlan says what kind of request the latest user message is, which kind
of source answers it, and how the answer is best presented. It is derived
deterministically from the conversation's messages: no model call, no database
access, no user or role. It is advice for orchestration and presentation only:
it never grants, denies or runs a tool (RBAC and Layer 4 do that, and mutations
belong to Layer 8), and it is never checkpointed, because the same messages
always produce the same plan.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

# Earlier user messages considered when a message continues the conversation ("Only for 132-2").
CONTEXT_MESSAGES = 10


class Intent(StrEnum):
    INFORMATIONAL = "informational"  # facts about specific records: items, stock, vendors, orders
    ANALYTICAL = "analytical"  # aggregates, comparisons, trends, KPIs
    KNOWLEDGE = "knowledge"  # descriptive catalogue and classification knowledge
    MIXED = "mixed"  # catalogue knowledge together with live business data
    OPERATIONAL = "operational"  # wants a change made; identified here, never executed (Layer 8)
    CONVERSATIONAL = "conversational"  # greetings, thanks, "what can you do"
    UNCLEAR = "unclear"


class Source(StrEnum):
    STRUCTURED_TOOL = "structured_tool"  # Layer 4 business tools
    SEMANTIC_RETRIEVAL = "semantic_retrieval"  # Layer 6 search_knowledge
    MIXED = "mixed"
    NONE = "none"
    CLARIFICATION_REQUIRED = "clarification_required"


class Presentation(StrEnum):
    TEXT = "text"
    TABLE = "table"
    CHART = "chart"
    SUMMARY = "summary"
    MIXED = "mixed"  # a short explanation plus structured data


class Confidence(StrEnum):
    HIGH = "high"  # the message itself names the request
    MEDIUM = "medium"  # read from conversational context, or a request whose subject was not recognised
    LOW = "low"  # could not be determined


class DataKind(StrEnum):
    RECORDS = "records"  # rows for a table
    CATEGORICAL = "categorical"  # one numeric row per category: a bar chart candidate
    TIME_SERIES = "time_series"  # rows over time: a line chart candidate


class ChartKind(StrEnum):
    BAR = "bar"
    LINE = "line"


class Dataset(BaseModel):
    """Rows inside one tool result that can back a table or chart. It points at the data; it never copies it."""

    model_config = ConfigDict(frozen=True)

    tool: str
    tool_call_id: str
    path: str  # dotted key inside the tool's "result"; "" when the result itself is the list
    kind: DataKind
    rows: int
    x: str | None = None  # label (categorical) or time (time series) field
    y: tuple[str, ...] = ()  # numeric fields
    series: str | None = None  # split rows by this field, never sum across it (e.g. currency or unit)
    chartable: bool = False  # the actual rows support a faithful chart (see _chartable)
    chart: ChartKind | None = None  # set on the datasets the plan decides to chart


class ResponsePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: Intent
    source: Source
    presentation: Presentation
    requested_presentation: Presentation | None = None  # what the user explicitly asked for, if anything
    confidence: Confidence
    clarification_required: bool = False
    missing: tuple[str, ...] = ()  # what the request does not say, e.g. ("vendor", "item")
    reason: str
    datasets: tuple[Dataset, ...] = ()


# --- tool result shapes ---------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    path: str
    kind: DataKind = DataKind.RECORDS
    x: str | None = None
    y: tuple[str, ...] = ()
    series: str | None = None
    # Word beginnings (regex fragments) naming what the dataset compares. Without an explicit request, a
    # comparison is charted only from a dataset the question is about. Datasets without topic words (capped,
    # unranked listings) are charted only on request: they cannot reliably answer "which is the most".
    topic: tuple[str, ...] = ()


_CAT = DataKind.CATEGORICAL

# Where each read tool's result holds rows. Tools with no entry rows hold none
# (one record, or descriptive text from search_knowledge, which is presented as text).
# Records with one label and one measure are categorical: they can be compared as bars, split
# by unit or currency so that quantities in different units are never drawn on one axis.
RESULT_SHAPES: dict[str, tuple[_Shape, ...]] = {
    "search_items": (_Shape(""),),
    "get_item_details": (_Shape("stock", _CAT, "location_name", ("quantity",), "unit", ("location", "warehouse")),),
    "list_locations": (_Shape(""),),
    "get_stock_by_location": (_Shape("stock", _CAT, "item_code", ("quantity",), "unit"),),  # capped, by code
    "get_transaction_history": (_Shape("", DataKind.TIME_SERIES, "created_at", ("quantity",), "item_code"),),
    # Sorted by quantity, lowest first: a ranking of the lowest stock positions.
    "get_low_stock_items": (_Shape("items", _CAT, "item_code", ("quantity",), "unit", ("item", "low")),),
    "list_vendors": (_Shape(""),),
    "get_vendor": (),
    "list_purchase_orders": (_Shape("", _CAT, "po_number", ("total_amount",), "currency"),),  # capped, newest first
    "get_purchase_order": (_Shape("lines"),),  # one order's detail: a table, never a chart
    "inventory_summary": (
        _Shape("stock_by_location", _CAT, "location_name", ("items_in_stock", "items_out_of_stock"),
               topic=("location", "warehouse")),
        _Shape("stock_by_category", _CAT, "category_name", ("active_items", "items_in_stock"), topic=("categor",)),
        _Shape("recent_transactions.by_type", _CAT, "transaction_type", ("transactions",),
               topic=("transaction", "movement")),
    ),
    "procurement_summary": (
        _Shape("purchase_orders_by_status", _CAT, "status", ("purchase_orders", "total_amount"), "currency",
               ("status",)),
        _Shape("open_purchase_orders"),  # one row per currency: amounts in different currencies are not compared
        _Shape("top_vendors_by_spend", _CAT, "vendor_name", ("total_amount",), "currency",
               ("vendor", "supplier", "spend")),
        _Shape("purchase_orders_by_month", DataKind.TIME_SERIES, "month", ("total_amount",), "currency",
               ("month", "trend", "time")),
    ),
    "procurement_overview": (
        _Shape("purchase_orders_by_status", _CAT, "status", ("purchase_orders", "total_amount"), "currency",
               ("status",)),
        _Shape("open_purchase_orders"),
    ),
    "search_knowledge": (),
}

_SHAPES_BY_KEY = {(tool, shape.path): shape for tool, shapes in RESULT_SHAPES.items() for shape in shapes}
_SUMMARY_TOOLS = frozenset({"inventory_summary", "procurement_summary", "procurement_overview"})
_DETAIL_TOOLS = frozenset({"get_item_details", "get_purchase_order"})


# --- cues (matched on lowercased text) --------------------------------------------


def _words(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


_PO_NUMBER = re.compile(r"\bpo-\d{4}-\d{4}\b")
_ITEM_CODE = re.compile(r"\b[a-z]{2}(?:-[a-z]{2,3}){0,2}-\d{4,5}\b")  # BO-MC-0100, RM-MT-FS-0012, NW-00012
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

_LIVE = _words(
    "stocks?", "on hand", "available", "availability", "quantit(?:y|ies)", "qty", "inventory", "warehouses?",
    "locations?", "vendors?", "suppliers?", "purchase orders?", "pos?", "orders?", "procurement", "purchas(?:e|es|ing)",
    "transactions?", "movements?", "receipts?", "payments?", "prices?", "costs?", "spend(?:ing)?", "low",
)
_CATALOGUE = _words(
    "items?", "products?", "parts?", "spares?", "materials?", "categor(?:y|ies)", "sub-?categor(?:y|ies)", "hsn",
    "catalog(?:ue)?",
)
_KNOWLEDGE = re.compile(
    r"\b(?:mean|means|meaning|define|definition|naming|be named|conventions?|classify|classified|classification|"
    r"belongs?|fall under|falls under|standards?|description|used for|purpose of|difference between|"
    r"kinds? of|types? of|something like|anything like|similar to|looks? like)\b"
    r"|\bwhat(?:'s| is| are) (?:a|an)\b|\b(?:which|what) (?:sub-?)?categor(?:y|ies)\b"
)
_ANALYTICAL = re.compile(
    r"\b(?:how many|how much|totals?|sum|count|number of|average|avg|trends?|over time|compare|compared|"
    r"comparison|versus|vs|breakdown|break down|distribution|kpis?|metrics?|statistics|stats|top|"
    r"most(?! recent)|least|highest|lowest|biggest|largest|smallest|spend|spending|growth|monthly|weekly|daily|"
    r"yearly|percent|percentage|ratio|analy[sz]e|analysis|performance|"
    r"per (?:month|week|day|year|location|category|vendor|status|type)|"
    r"by (?:month|week|day|year|location|category|vendor|status|type|currency)|"
    r"(?:this|last) (?:month|week|year|quarter))\b"
)
_OVERVIEW = _words("summary", "summari[sz]e", "overview", "overall")  # analytical only with a business subject
# The analytic form of a question decides whether a chart helps, whatever words ask for it:
# a trend is read along time (line), a comparison or ranking across categories (bar). A part-to-whole
# question ("distribution of statuses") is a comparison of its parts and is drawn as bars too.
_TREND = _words("trends?", "over time", "monthly", "weekly", "daily", "yearly", "growth", "chang(?:e|ed|es|ing)",
                "per (?:month|week|day|year)", "by (?:month|week|day|year)",
                r"(?:last|past) (?:\d+ )?(?:days|weeks|months|years|quarters?)")
_COMPARE = _words(
    "compare[ds]?", "comparing", "comparison", "versus", "vs", "across", "relative to", "breakdown", "break down",
    "distribution", "distributed", "split", "share", "proportions?", "composition",
    "most(?! recent)", "least", "highest", "lowest", "biggest", "largest", "smallest", "top", "bottom", "best",
    "worst", "rank(?:ed|ing|s)?",
    "(?:per|by) (?:location|warehouse|category|vendor|supplier|status|type|item|currency)",
)
_SINGLE_VALUE = _words("how many", "how much", "number of", "count", "total")

_PO_ACTION = re.compile(
    r"\b(?:create|raise|place|draft|prepare|make|submit|book)\b[^.?!]*\b(?:purchase orders?|pos?|orders?)\b"
    r"|^(?:please |kindly )?(?:(?:can|could) you |i (?:want|need) to |let'?s )?order\b"
)
_STOCK_ACTION = re.compile(
    r"\b(?:record|log|receive|issue|remove|deduct|adjust|update|increase|decrease|reduce|set|consume|write off)\b"
    r"[^.?!]*\b(?:stocks?|inventory|units?|pcs|pieces|nos|quantity|qty|kgs?)\b"
    r"|\b(?:receive|issue|remove|deduct|add|consume)\s+\d"
)
# Goods received against a purchase order: receipt wording together with a PO reference and a quantity
# ("We received 6 pcs for PO-2026-0057"). Without a PO reference it stays a stock movement.
_RECEIPT_WORD = _words("receive[ds]?", "receiving", "arrived", "delivered")
_RECEIPT_ACTION = re.compile(r"\b(?:record|log|enter|book|post)(?:ing)? (?:a |the )?(?:goods )?receipts?\b")
_PO_REFERENCE = re.compile(r"\bpo-\d{4}-\d{4}\b|\b(?:purchase orders?|pos?)\b")
# "Which vendors do we place most orders with?" or "How do I record a receipt?" ask about a change, not for one.
_QUESTION = re.compile(r"^(?:which|what|who|where|when|why|how|do|does|did|is|are|was|were|has|have)\b")
PURCHASE_ORDER = "purchase_order"
STOCK_MOVEMENT = "stock_movement"
PURCHASE_RECEIPT = "purchase_receipt"
_OPERATION_LABELS = {
    PURCHASE_ORDER: "create a purchase order",
    STOCK_MOVEMENT: "record a stock movement",
    PURCHASE_RECEIPT: "record goods received against a purchase order",
}

# Details an operation needs. Detection is only good enough to notice what is plainly absent;
# real validation stays with Layer 4 (and Layer 8 for mutations).
_ITEM_REF = re.compile(
    r"\b(?:item|items|of)\s+(?!(?:the|a|an|it|them)\b)\S+"
    r"|\b\d+(?:\.\d+)?\s*(?:units?|pcs|pieces|nos|kgs?|boxes|sets?)?\s+(?!(?:at|in|to|into|from|for|on|units?|pcs)\b)"
    r"[a-z]{3,}"
)
_VENDOR_REF = re.compile(r"\b(?:from|vendor|supplier|with)\s+(?!(?:the|a|an)\s+(?:vendor|supplier)\b)\S+")
_PLACE_REF = re.compile(
    r"\b(?:at|in|into|to|from)\s+(?:the\s+)?(?:location\s+|warehouse\s+)?(?!(?:stock|inventory)\b)\S+"
)
_DIRECTION = _words("receive[ds]?", "receiving", "add(?:ed)?", "inbound", "increase", "issue[ds]?", "remove[ds]?",
                    "deduct(?:ed)?", "consume[ds]?", "outbound", "decrease", "reduce", "write off")

_STOCK_SUBJECT = _words("stocks?", "inventory", "on hand", "quantit(?:y|ies)", "qty")
_OTHER_SUBJECT = _words("vendors?", "suppliers?", "purchase orders?", "pos?", "orders?", "transactions?",
                        "movements?", "receipts?", "payments?", "prices?", "locations?", "warehouses?")
_STOCK_SCOPE = re.compile(
    r"\b(?:at|in|for|of|from|across|on)\s+(?!(?:the\s+)?(?:stock|inventory)\b|hand\b)\S+"
    r"|\b(?:low|all|every|each|overall|total|whole|entire|zero|negative|out of stock|locations?|warehouses?)\b"
)

_REQUEST = re.compile(
    r"^(?:please |kindly )?(?:(?:can|could) you )?(?:find|search|look ?up|show|list|get|give|tell|check|display|"
    r"fetch|which|what|where|who|when|do we|is there|are there|any)\b"
)
_CONVERSATIONAL = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|thx|ok|okay|great|cool|nice|bye|goodbye|good (?:morning|afternoon|evening))"
    r"(?: there| you| so much| a lot)?[\s!.,]*$"
    r"|^(?:help|what can you do|how can you help(?: me)?|who are you)[\s?!.]*$"
)

_TABLE = _words("table", "tables", "tabular", "tabulate", "spreadsheet", "grid")
_CHART = _words("charts?", "graphs?", "plot", "plotted", "visuali[sz]e", "visuali[sz]ation", "histogram", "pie")
_SUMMARY = _words("summary", "summari[sz]e", "overview", "brief", "briefly", "tl;?dr", "kpis?", "in short")


@dataclass(frozen=True)
class _Cues:
    operation: str | None
    analytical: bool
    knowledge: bool
    live: bool
    domain: bool
    request: bool
    conversational: bool


def _cues(text: str) -> _Cues:
    without_po = _PO_NUMBER.sub(" ", text)
    codes = bool(_PO_NUMBER.search(text) or _ITEM_CODE.search(without_po))
    live = bool(_LIVE.search(text) or _PO_NUMBER.search(text))
    domain = live or codes or bool(_CATALOGUE.search(text))
    operation = None
    if not _QUESTION.search(text):
        if _PO_ACTION.search(text):
            operation = PURCHASE_ORDER
        elif _is_receipt(text):
            operation = PURCHASE_RECEIPT
        elif _STOCK_ACTION.search(text):
            operation = STOCK_MOVEMENT
    return _Cues(
        operation=operation,
        analytical=bool(_ANALYTICAL.search(text) or (domain and _OVERVIEW.search(text))),
        knowledge=bool(_KNOWLEDGE.search(text)),
        live=live,
        domain=domain,
        request=bool(_REQUEST.search(text)),
        conversational=bool(_CONVERSATIONAL.search(text)),
    )


def _is_receipt(text: str) -> bool:
    """A statement (not a request to show something) of a quantity received against a purchase order."""
    return bool(
        not _REQUEST.search(text)
        and (_RECEIPT_WORD.search(text) or _RECEIPT_ACTION.search(text))
        and _PO_REFERENCE.search(text)
        and _NUMBER.search(_PO_NUMBER.sub(" ", text))
    )


def _requested_presentation(text: str) -> Presentation | None:
    table, chart = _TABLE.search(text), _CHART.search(text)
    if table and chart:
        return Presentation.MIXED
    if chart:
        return Presentation.CHART
    if table:
        return Presentation.TABLE
    if _SUMMARY.search(text):
        return Presentation.SUMMARY
    return None


# --- intent -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Turn:
    intent: Intent
    source: Source
    confidence: Confidence
    reason: str
    missing: tuple[str, ...] = ()
    operation: str | None = None
    texts: tuple[str, ...] = ()  # this message plus the earlier ones it continues
    inherited: bool = False


def _classify(text: str, previous: _Turn | None, prior_text: str | None) -> _Turn:
    cues = _cues(text)
    context = previous if previous and previous.intent not in (Intent.CONVERSATIONAL, Intent.UNCLEAR) else None
    continued = ((context.texts if context else ()) + (text,))[-CONTEXT_MESSAGES:]

    if cues.operation:
        return _operational(cues.operation, (text,), Confidence.HIGH, f"asks to {_OPERATION_LABELS[cues.operation]}")
    if (
        context and context.intent is Intent.OPERATIONAL and context.missing
        and not (cues.analytical or cues.knowledge or cues.conversational)
    ):
        return _operational(context.operation, continued, Confidence.MEDIUM, "adds details to the requested change",
                            inherited=True)
    if cues.knowledge and (cues.live or cues.analytical):
        return _Turn(Intent.MIXED, Source.MIXED, Confidence.HIGH,
                     "asks for catalogue knowledge together with live business data", texts=(text,))
    if cues.knowledge:
        return _Turn(Intent.KNOWLEDGE, Source.SEMANTIC_RETRIEVAL, Confidence.HIGH,
                     "asks about descriptive catalogue or classification knowledge", texts=(text,))
    if cues.analytical and cues.domain:
        return _Turn(Intent.ANALYTICAL, Source.STRUCTURED_TOOL, Confidence.HIGH,
                     "asks for aggregates, comparisons or trends", texts=(text,))
    if cues.domain:
        return _informational((text,), prior_text, Confidence.HIGH, "asks about specific business records")
    if cues.conversational:
        return _Turn(Intent.CONVERSATIONAL, Source.NONE, Confidence.HIGH, "greeting or small talk", texts=(text,))
    if context:
        if cues.analytical:
            return _Turn(Intent.ANALYTICAL, Source.STRUCTURED_TOOL, Confidence.MEDIUM,
                         "aggregate follow-up to the previous request", texts=continued, inherited=True)
        if context.intent is Intent.INFORMATIONAL:
            return _informational(continued, None, Confidence.MEDIUM, "follow-up to the previous request",
                                  inherited=True)
        if context.intent is Intent.OPERATIONAL:
            return _operational(context.operation, continued, Confidence.MEDIUM, "follow-up to the requested change",
                                inherited=True)
        return replace(context, confidence=Confidence.MEDIUM, reason="follow-up to the previous request",
                       texts=continued, inherited=True)
    if cues.request:
        return _Turn(Intent.INFORMATIONAL, Source.STRUCTURED_TOOL, Confidence.MEDIUM,
                     "asks for information; the subject was not recognised", texts=(text,))
    return _Turn(Intent.UNCLEAR, Source.CLARIFICATION_REQUIRED, Confidence.LOW,
                 "the request could not be determined", texts=(text,))


def _informational(
    texts: tuple[str, ...], prior_text: str | None, confidence: Confidence, reason: str, inherited: bool = False
) -> _Turn:
    """Stock questions need an item or location ("Show me the stock") unless the conversation gives one."""
    stock_question = any(_STOCK_SUBJECT.search(t) and not _OTHER_SUBJECT.search(t) for t in texts)
    scoped = any(_has_stock_scope(t) for t in (*texts, prior_text or ""))
    missing = ("item or location",) if stock_question and not scoped else ()
    return _Turn(Intent.INFORMATIONAL, Source.STRUCTURED_TOOL, confidence, reason, missing=missing, texts=texts,
                 inherited=inherited)


def _has_stock_scope(text: str) -> bool:
    return bool(_STOCK_SCOPE.search(text) or _PO_NUMBER.search(text) or _ITEM_CODE.search(text))


def _operational(
    operation: str | None, texts: tuple[str, ...], confidence: Confidence, reason: str, inherited: bool = False
) -> _Turn:
    return _Turn(Intent.OPERATIONAL, Source.STRUCTURED_TOOL, confidence, reason,
                 missing=_operation_missing(operation, " ".join(texts)), operation=operation, texts=texts,
                 inherited=inherited)


def _operation_missing(operation: str | None, text: str) -> tuple[str, ...]:
    without_po = _PO_NUMBER.sub(" ", text)
    without_codes = _ITEM_CODE.sub(" ", without_po)
    present = {
        "item": bool(_ITEM_CODE.search(without_po) or _ITEM_REF.search(without_codes)),
        "quantity": bool(_NUMBER.search(without_codes)),
    }
    if operation == PURCHASE_ORDER:
        present = {"vendor": bool(_VENDOR_REF.search(text)), **present}
    elif operation == STOCK_MOVEMENT:
        present |= {"location": bool(_PLACE_REF.search(text)), "direction": bool(_DIRECTION.search(text))}
    elif operation == PURCHASE_RECEIPT:
        # The item can be read from the PO's lines, so only what the user alone knows is required.
        present = {"purchase order": bool(_PO_NUMBER.search(text)), "quantity": present["quantity"],
                   "location": bool(_PLACE_REF.search(text))}
    return tuple(field for field, found in present.items() if not found)


# --- data and presentation ----------------------------------------------------------


def _tool_results(window: Sequence[AnyMessage]) -> list[tuple[ToolMessage, Any]]:
    """Successful tool results in a slice of the conversation. Errors and unreadable content carry no data."""
    results = []
    for message in window:
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and "result" in payload:
            results.append((message, payload["result"]))
    return results


def _datasets(results: list[tuple[ToolMessage, Any]]) -> list[Dataset]:
    found = []
    for message, result in results:
        for shape in RESULT_SHAPES.get(message.name, ()):
            rows = _at(result, shape.path)
            if isinstance(rows, list) and rows:
                found.append(
                    Dataset(tool=message.name, tool_call_id=message.tool_call_id, path=shape.path, kind=shape.kind,
                            rows=len(rows), x=shape.x, y=shape.y, series=shape.series,
                            chartable=_chartable(shape, rows))
                )
    return found


# Limits the frontend draws within (frontend/src/lib/visualize.ts); a dataset beyond them is shown as a table.
MAX_BAR_PANELS = 4
MAX_LINE_SERIES = 6
# Bars a reader can compare at a glance. A longer list is clearer as a sorted table, so a chart is drawn
# for more categories only when the user asks for one.
MAX_AUTO_CATEGORIES = 20
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and _NUMERIC.match(value.strip()):
        return float(value)
    return None


def _chartable(shape: _Shape, rows: list[Any]) -> bool:
    """Whether these rows can be drawn faithfully and the chart would show something.

    Every row needs its label (or time) and numeric measures. A chart of identical values compares
    nothing, so the values must differ. Bars grow from zero, so they need non-negative values and one
    bar per label within a series (repeated labels, such as one item at several locations, would be
    ambiguous). Series (units, currencies) are drawn apart and never on one axis.
    """
    if shape.kind is DataKind.RECORDS or shape.x is None or not shape.y:
        return False
    groups: dict[Any, list[tuple[Any, list[float]]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return False
        label = row.get(shape.x)
        values = [_number(row.get(key)) for key in shape.y]
        if label in (None, "") or None in values:
            return False
        if shape.kind is DataKind.TIME_SERIES and not (isinstance(label, str) and _TIMESTAMP.match(label)):
            return False
        groups.setdefault(row.get(shape.series) if shape.series else None, []).append((label, values))
    values = [value for group in groups.values() for _, measures in group for value in measures]
    if len(rows) < 2 or len(set(values)) < 2:
        return False
    if shape.kind is DataKind.TIME_SERIES:
        return len(groups) <= MAX_LINE_SERIES
    panels = len(groups) * (len(shape.y) if len(shape.y) > 1 else 1)  # a conservative bound on the frontend's panels
    return (
        panels <= MAX_BAR_PANELS
        and min(values) >= 0
        and all(len({label for label, _ in group}) == len(group) for group in groups.values())
    )


def _shape_of(dataset: Dataset) -> _Shape:
    return _SHAPES_BY_KEY[(dataset.tool, dataset.path)]


def _at(value: Any, path: str) -> Any:
    for key in filter(None, path.split(".")):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def analytic_form(text: str) -> str | None:
    """"trend", "comparison" or None: what the question asks the data to show, if a chart can show it."""
    if _TREND.search(text):
        return "trend"
    if _COMPARE.search(text):
        return "comparison"
    return None


def _about(dataset: Dataset, text: str) -> bool:
    topic = _shape_of(dataset).topic
    return bool(topic) and re.search(r"\b(?:" + "|".join(topic) + ")", text) is not None


def _charts_for(datasets: list[Dataset], text: str, form: str | None, explicit: bool) -> frozenset[int]:
    """Which datasets to chart: those whose rows can show what the question asks.

    A trend needs a time series, and every time series can show one. A comparison needs categories,
    and without an explicit request only a dataset the question is about qualifies (its topic words),
    with at most MAX_AUTO_CATEGORIES bars: a chart of something else would answer a different
    question. An explicit request accepts either kind and any number of bars, preferring the datasets
    the question is about and otherwise charting every chartable one.
    """
    kinds = {"trend": (DataKind.TIME_SERIES,), "comparison": (DataKind.CATEGORICAL,)}.get(
        form, (DataKind.TIME_SERIES, DataKind.CATEGORICAL)
    )
    candidates = [i for i, d in enumerate(datasets) if d.chartable and d.kind in kinds]
    topical = [i for i in candidates if _about(datasets[i], text)]
    if explicit:
        return frozenset(topical or candidates)
    if form == "trend":
        return frozenset(candidates)
    return frozenset(i for i in topical if datasets[i].rows <= MAX_AUTO_CATEGORIES)


def _presentation(
    turn: _Turn, requested: Presentation | None, datasets: list[Dataset], used: set[str]
) -> tuple[Presentation, frozenset[int]]:
    """The presentation, and which datasets are charted.

    Conservative: tables need rows, charts need rows that can be drawn faithfully (Dataset.chartable),
    and nothing is visualised without data. A chart is chosen when the question compares, ranks or
    follows something over time and a returned dataset can show exactly that; an explicit request for
    a chart is honoured whenever the data supports one. Detail lookups, single figures and data a chart
    cannot show stay tables, figures or text.
    """
    none: frozenset[int] = frozenset()
    if turn.intent in (Intent.OPERATIONAL, Intent.CONVERSATIONAL, Intent.UNCLEAR) or turn.missing:
        return Presentation.TEXT, none
    listed = [d for d in datasets if d.rows >= 2]
    fallback = Presentation.TABLE if listed else Presentation.TEXT
    text = " ".join(turn.texts)
    if requested in (Presentation.CHART, Presentation.MIXED):
        charts = _charts_for(datasets, text, analytic_form(text), explicit=True)
        return (requested, charts) if charts else (fallback, none)
    if requested is Presentation.TABLE:
        return (Presentation.TABLE if datasets else Presentation.TEXT), none
    if requested is Presentation.SUMMARY:
        return Presentation.SUMMARY, none
    if turn.intent is Intent.KNOWLEDGE:
        return Presentation.TEXT, none
    form = analytic_form(text)
    if form:
        charts = _charts_for(datasets, text, form, explicit=False)
        if charts:
            return Presentation.CHART, charts
    if turn.intent is Intent.MIXED:
        return (Presentation.MIXED if listed else Presentation.TEXT), none
    if turn.intent is Intent.ANALYTICAL:
        if used & _SUMMARY_TOOLS:
            mixed = any(d.tool not in _SUMMARY_TOOLS for d in listed)
            return (Presentation.MIXED if mixed else Presentation.SUMMARY), none
        if used and _SINGLE_VALUE.search(text):
            return Presentation.SUMMARY, none
        return fallback, none
    if any(d.tool in _DETAIL_TOOLS for d in listed):
        return Presentation.MIXED, none  # one record explained, its rows in a table
    return fallback, none


def _mark_charts(datasets: list[Dataset], charts: frozenset[int]) -> tuple[Dataset, ...]:
    return tuple(
        d.model_copy(update={"chart": ChartKind.LINE if d.kind is DataKind.TIME_SERIES else ChartKind.BAR})
        if i in charts else d
        for i, d in enumerate(datasets)
    )


# --- entry point --------------------------------------------------------------------


def plan_turn(messages: Sequence[AnyMessage]) -> ResponsePlan:
    """Plan for the latest user message, using earlier messages as context and this turn's tool results as data."""
    humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not humans:
        return ResponsePlan(intent=Intent.UNCLEAR, source=Source.CLARIFICATION_REQUIRED,
                            presentation=Presentation.TEXT, confidence=Confidence.LOW, clarification_required=True,
                            reason="there is no user message")

    turn: _Turn | None = None
    text: str | None = None
    for index in humans[-CONTEXT_MESSAGES:]:
        current = _normalize(messages[index].text)
        turn, text = _classify(current, turn, text), current
    assert turn is not None and text is not None

    window = messages[humans[-1] + 1:]
    results = _tool_results(window)
    # A comparison answered from the conversation without any tool call ("Which location has the most?"
    # after a stock lookup) compares the previous turn's rows; only a dataset it is about can be charted.
    from_history = analytic_form(text) == "comparison" and not any(isinstance(m, ToolMessage) for m in window)
    reused = not results and (turn.inherited or from_history) and len(humans) > 1
    if reused:
        # "Put that in a table" answered without a new tool call: the data is the previous turn's.
        results = _tool_results(messages[humans[-2] + 1 : humans[-1]])
    datasets = _datasets(results)
    if reused and not turn.inherited:
        datasets = [d for d in datasets if _about(d, text)]
    requested = _requested_presentation(text)
    presentation, charts = _presentation(turn, requested, datasets, {m.name for m, _ in results})

    return ResponsePlan(
        intent=turn.intent,
        source=turn.source,
        presentation=presentation,
        requested_presentation=requested,
        confidence=turn.confidence,
        clarification_required=turn.intent is Intent.UNCLEAR or bool(turn.missing),
        missing=turn.missing,
        reason=turn.reason,
        datasets=_mark_charts(datasets, charts),
    )


def _normalize(text: Any) -> str:
    return " ".join(str(text).lower().replace("’", "'").split())
