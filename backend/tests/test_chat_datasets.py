"""Layer 14: rows behind the response plan, serialized for the browser (app.agent.datasets).

Unit tests build conversations from messages and plan them with the real Layer 7 planner;
API tests run the real agent graph behind POST /api/chat with a scripted model and a fake
tool executor (no database, no model provider).
"""

import json
import re
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import graph as agent_graph
from app.agent import tools as agent_tools
from app.agent.datasets import COLUMNS, MAX_DATASETS, MAX_ROWS, MAX_TEXT_LENGTH, ChatData, chat_data
from app.agent.planning import RESULT_SHAPES, Dataset, DataKind, Intent, Presentation, ResponsePlan, Source, plan_turn
from app.auth.permissions import Role
from tests.conftest import assert_no_sensitive_data
from tests.fakes import tool_call
from tests.test_chat_api import URL, agent, auth_engine, login, send  # noqa: F401 (fixtures)
from tests.test_chat_api import MOVE, VALID, layer8  # noqa: F401 (fixtures)

USER_ID = str(uuid.uuid4())

# A low-stock row as the repository returns it (after json.dumps(default=str) in the executor).
LOW_STOCK_ROW = {
    "item_id": str(uuid.uuid4()),
    "item_code": "BO-MC-0100",
    "item_name": "Hex bolt M10",
    "unit": "pcs",
    "location_id": str(uuid.uuid4()),
    "location_name": "132-1",
    "quantity": "0.000",
    "min_stock_level": None,
    "effective_threshold": "0",
    "threshold_source": "fallback",
    "last_transaction_at": "2026-10-01 09:30:00",
}


def tool_result(call_id: str, name: str, result, status: str = "success") -> ToolMessage:
    return ToolMessage(content=json.dumps({"result": result}), tool_call_id=call_id, name=name, status=status)


def turn(question: str, name: str, result, call_id: str = "call_1", answer: str = "Here you go.") -> list:
    return [HumanMessage(content=question), tool_call(name, {}, call_id), tool_result(call_id, name, result),
            AIMessage(content=answer)]


def low_stock(n: int) -> dict:
    return {"fallback_threshold": "0", "items": [{**LOW_STOCK_ROW, "item_code": f"BO-{i:04d}"} for i in range(n)]}


def data_for(messages) -> ChatData:
    return chat_data(messages, plan_turn(messages))


# --- serialization -----------------------------------------------------------------------------------


def test_rows_are_copied_through_the_column_allowlist():
    messages = turn("Show me low-stock items as a table", "get_low_stock_items", low_stock(3))
    data = data_for(messages)

    assert len(data.datasets) == 1
    payload = data.datasets[0]
    assert payload.tool == "get_low_stock_items" and payload.title == "Low-stock items"
    # Low-stock rows can be compared as bars (item, quantity per unit); a requested table is still a table.
    assert payload.kind is DataKind.CATEGORICAL and payload.chart is None
    assert [c.key for c in payload.columns] == ["item_code", "item_name", "location_name", "quantity", "unit",
                                                 "effective_threshold"]
    assert payload.rows[0] == {"item_code": "BO-0000", "item_name": "Hex bolt M10", "location_name": "132-1",
                               "quantity": "0.000", "unit": "pcs", "effective_threshold": "0"}
    assert payload.total_rows == 3 and payload.truncated is False


def test_ids_and_internal_fields_never_leave_the_server():
    row = {**LOW_STOCK_ROW, "created_by": USER_ID, "notes": "internal", "password_hash": "$2b$x",
           "token": "secret", "specifications": {"nested": True}}
    data = data_for(turn("Show low stock items", "get_low_stock_items", {"items": [row, row]}))
    text = data.model_dump_json()
    for leak in (LOW_STOCK_ROW["item_id"], LOW_STOCK_ROW["location_id"], USER_ID, "internal", "$2b$", "secret",
                 "nested", "call_1", "tool_call_id", "threshold_source"):
        assert leak not in text
    assert_no_sensitive_data(text)


@pytest.mark.parametrize(("role", "visible"), [(Role.INVENTORY_MANAGER, False), (Role.PROCUREMENT_MANAGER, True),
                                               (Role.OWNER, True)])
def test_transaction_dataset_carries_only_what_the_service_returned(monkeypatch, role, visible):
    # Layer 4 redacts purchase-order receipt fields for roles without procurement:read; the dataset is built
    # from that same tool result, so it cannot bring the purchase price or notes back.
    from app.auth.permissions import AuthenticatedUser
    from app.services import inventory

    po_receipt = {
        "id": "txn-1", "item_id": "item-1", "item_code": "BO-MC-0172", "item_name": "Bolt", "location_id": "loc-1",
        "location_name": "103-1", "transaction_type": "inbound", "quantity": 6.0, "unit_cost": 18.06,
        "currency": "INR", "reference_type": "purchase_order", "reference_id": "po-secret-id",
        "transfer_location_id": None, "issued_to_id": None, "issued_to_name": None, "notes": "invoice no 2307",
        "created_by": USER_ID, "created_at": "2026-10-01 09:30:00",
    }
    monkeypatch.setattr(inventory.repo, "get_transactions", lambda conn, **kw: [dict(po_receipt), dict(po_receipt)])
    user = AuthenticatedUser(id=uuid.uuid4(), email="u@test.local", full_name="Test", role=role)
    result = json.loads(json.dumps(inventory.get_transaction_history(None, user), default=str))

    data = data_for(turn("Show recent stock transactions as a table", "get_transaction_history", result))

    [payload] = data.datasets
    assert payload.tool == "get_transaction_history"
    assert all(row["quantity"] == 6.0 and row["item_code"] == "BO-MC-0172" for row in payload.rows)
    assert [(row["unit_cost"], row["currency"]) for row in payload.rows] == (
        [(18.06, "INR")] * 2 if visible else [(None, None)] * 2
    )
    text = data.model_dump_json()
    assert ("18.06" in text) is visible
    for leak in ("po-secret-id", "invoice no 2307", "purchase_order"):
        assert leak not in text


def test_no_allowlisted_column_is_an_identifier_or_user_reference():
    # Every rule, not only the planned shapes: a new rule cannot quietly send ids or user data.
    forbidden = re.compile(r"^id$|_id$|created_by|user|password|token|secret|thread|notes")
    exposed = {(tool, path, c.key) for (tool, path), (_, columns) in COLUMNS.items() for c in columns}
    assert not {entry for entry in exposed if forbidden.search(entry[2])}


def test_tool_arguments_never_reach_the_payload():
    marker = "ARG-7f3a"
    messages = [HumanMessage(content="Show low stock items as a table"),
                tool_call("get_low_stock_items", {"query": marker, "location": "/srv/app/main.py"}, "call_1"),
                tool_result("call_1", "get_low_stock_items", low_stock(2)), AIMessage(content="Two items.")]
    data = data_for(messages)
    assert data.datasets and marker not in data.model_dump_json() and "/srv/app" not in data.model_dump_json()


def test_only_scalar_cells_are_sent_and_long_text_is_shortened():
    row = {**LOW_STOCK_ROW, "item_name": "x" * 500, "quantity": {"nested": 1}, "unit": ["a"]}
    payload = data_for(turn("Show low stock items", "get_low_stock_items", {"items": [row, row]})).datasets[0]
    assert len(payload.rows[0]["item_name"]) == MAX_TEXT_LENGTH and payload.rows[0]["item_name"].endswith("…")
    assert payload.rows[0]["quantity"] is None and payload.rows[0]["unit"] is None


def test_rows_are_bounded_and_truncation_is_reported():
    payload = data_for(turn("Show low stock items", "get_low_stock_items", low_stock(MAX_ROWS + 25))).datasets[0]
    assert len(payload.rows) == MAX_ROWS
    assert payload.total_rows == MAX_ROWS + 25 and payload.truncated is True
    assert payload.rows[0]["item_code"] == "BO-0000"  # order kept


def test_every_planned_shape_has_a_rule_covering_its_chart_fields():
    for tool, shapes in RESULT_SHAPES.items():
        for shape in shapes:
            assert (tool, shape.path) in COLUMNS, (tool, shape.path)
            keys = {c.key for c in COLUMNS[(tool, shape.path)][1]}
            needed = {k for k in (shape.x, shape.series, *shape.y) if k}
            assert needed <= keys, (tool, shape.path, needed - keys)
            assert not any(k == "id" or k.endswith("_id") for k in keys)


def test_summary_figures_currencies_and_chart_marks_are_kept():
    summary = {
        "total_vendors": 34, "active_vendors": 33,
        "purchase_orders_by_status": [
            {"status": "placed", "currency": "INR", "purchase_orders": 32, "total_amount": "4437362.15"},
            {"status": "placed", "currency": "USD", "purchase_orders": 1, "total_amount": "542.10"},
        ],
        "open_purchase_orders": [{"currency": "INR", "purchase_orders": 35, "total_amount": "4788746.73",
                                  "lines_pending_delivery": 66}],
        "top_vendors_by_spend": [
            {"vendor_id": str(uuid.uuid4()), "vendor_name": "Sterling", "currency": "INR", "purchase_orders": 2,
             "total_amount": "3021209.47"},
            {"vendor_id": str(uuid.uuid4()), "vendor_name": "Acme", "currency": "USD", "purchase_orders": 1,
             "total_amount": "542.10"},
        ],
    }
    data = data_for(turn("Compare spend by vendor in a chart", "procurement_summary", summary))

    by_title = {d.title: d for d in data.datasets}
    vendors = by_title["Top vendors by spend"]
    assert vendors.chart == "bar" and vendors.series == "currency" and vendors.x == "vendor_name"
    assert [r["currency"] for r in vendors.rows] == ["INR", "USD"]
    assert [r["total_amount"] for r in vendors.rows] == ["3021209.47", "542.10"]  # exact decimals
    assert [(m.label, m.value) for m in data.metrics] == [("Vendors", 34), ("Active vendors", 33)]


def test_nested_metric_paths_are_read():
    summary = {"total_items": 501, "active_items": 500, "active_locations": 5,
               "low_stock": {"count": 149, "fallback_threshold": "0"},
               "stock_by_location": [{"location_id": "x", "location_name": "132-1", "stocked_items": 2,
                                      "items_in_stock": 1, "items_out_of_stock": 1},
                                     {"location_id": "y", "location_name": "103-1", "stocked_items": 2,
                                      "items_in_stock": 2, "items_out_of_stock": 0}],
               "stock_by_category": [], "recent_transactions": {"days": 30, "by_type": []}}
    data = data_for(turn("Give me an inventory summary", "inventory_summary", summary))
    assert ("Low-stock positions", 149) in [(m.label, m.value) for m in data.metrics]
    assert [d.title for d in data.datasets] == ["Stock by location"]


# --- what is read, and when ---------------------------------------------------------------------------


def test_text_answers_carry_no_data():
    messages = turn("What is a chequered plate?", "search_knowledge", {"results": [{"title": "x"}]})
    assert data_for(messages) == ChatData()


def test_only_tool_calls_named_by_the_plan_are_read():
    messages = [
        *turn("Show low stock items", "get_low_stock_items", low_stock(2), call_id="old"),
        HumanMessage(content="Show me the vendors"),
        tool_call("list_vendors", {}, "new"),
        tool_result("new", "list_vendors", [{"id": "v1", "name": "Acme", "code": "A1", "city": "Pune"},
                                            {"id": "v2", "name": "Bolt Co", "code": "B2", "city": "Delhi"}]),
        AIMessage(content="Two vendors."),
    ]
    data = data_for(messages)
    assert [d.tool for d in data.datasets] == ["list_vendors"]  # the earlier turn's rows are not included
    assert "BO-0000" not in data.model_dump_json()


def test_follow_up_without_new_tool_call_uses_the_previous_results():
    messages = [*turn("Show low stock items", "get_low_stock_items", low_stock(3)),
                HumanMessage(content="put that in a table"), AIMessage(content="| code |")]
    data = data_for(messages)
    assert [d.tool for d in data.datasets] == ["get_low_stock_items"] and data.datasets[0].total_rows == 3


@pytest.mark.parametrize(
    "result_message",
    [
        ToolMessage(content="not json", tool_call_id="call_1", name="get_low_stock_items"),
        ToolMessage(content=json.dumps({"error": "boom"}), tool_call_id="call_1", name="get_low_stock_items",
                    status="error"),
        tool_result("call_1", "get_low_stock_items", {"items": "not a list"}),
        tool_result("call_1", "list_vendors", low_stock(2)),  # the id names a different tool
    ],
)
def test_malformed_results_are_skipped(result_message):
    plan = plan_turn(turn("Show low stock items", "get_low_stock_items", low_stock(2)))
    messages = [HumanMessage(content="Show low stock items"), tool_call("get_low_stock_items", {}, "call_1"),
                result_message, AIMessage(content="ok")]
    assert chat_data(messages, plan).datasets == ()


def test_non_object_rows_are_dropped_and_unknown_shapes_skipped():
    payload = data_for(turn("Show low stock items", "get_low_stock_items",
                            {"items": [LOW_STOCK_ROW, "junk", 3, LOW_STOCK_ROW]})).datasets[0]
    assert payload.total_rows == 2 and len(payload.rows) == 2

    unknown = Dataset(tool="mystery_tool", tool_call_id="call_1", path="", kind=DataKind.RECORDS, rows=2)
    plan = ResponsePlan(intent=Intent.INFORMATIONAL, source=Source.STRUCTURED_TOOL, presentation=Presentation.TABLE,
                        confidence="high", reason="test", datasets=(unknown,))
    messages = [HumanMessage(content="x"), tool_result("call_1", "mystery_tool", [{"a": 1}, {"a": 2}])]
    assert chat_data(messages, plan) == ChatData()


def test_dataset_count_is_bounded():
    datasets = tuple(Dataset(tool="list_vendors", tool_call_id=f"c{i}", path="", kind=DataKind.RECORDS, rows=2)
                     for i in range(MAX_DATASETS + 3))
    plan = ResponsePlan(intent=Intent.INFORMATIONAL, source=Source.STRUCTURED_TOOL, presentation=Presentation.TABLE,
                        confidence="high", reason="test", datasets=datasets)
    messages = [tool_result(f"c{i}", "list_vendors", [{"name": "A"}, {"name": "B"}]) for i in range(MAX_DATASETS + 3)]
    assert len(chat_data(messages, plan).datasets) == MAX_DATASETS


# --- through POST /api/chat ------------------------------------------------------------------------------


@pytest.fixture
def counted_tools(monkeypatch):
    """Fake read-tool executor that records every execution."""
    results, calls = {}, []

    def fake_execute(user, name, args, **kwargs):
        calls.append((user.id, name))
        return agent_tools.ToolResult(json.dumps({"result": results[name]}), is_error=False)

    monkeypatch.setattr(agent_graph, "execute_tool", fake_execute)
    return results, calls


def test_api_returns_rows_from_the_single_tool_execution(client, login, agent, counted_tools):
    results, calls = counted_tools
    results["get_low_stock_items"] = low_stock(3)
    agent([tool_call("get_low_stock_items", {}), AIMessage(content="Three items are low.")])
    user, headers = login(Role.INVENTORY_MANAGER)

    response = send(client, headers, uuid.uuid4(), "Show me low-stock items")
    body = response.json()

    assert calls == [(user.id, "get_low_stock_items")]  # executed once, for the authenticated user
    assert body["plan"]["presentation"] == "table"
    [payload] = body["data"]["datasets"]
    assert payload["title"] == "Low-stock items" and payload["total_rows"] == 3 and payload["truncated"] is False
    assert [row["item_code"] for row in payload["rows"]] == ["BO-0000", "BO-0001", "BO-0002"]
    assert set(payload) == {"tool", "title", "kind", "x", "y", "series", "chart", "columns", "rows", "total_rows",
                            "truncated"}
    for internal in (LOW_STOCK_ROW["item_id"], str(user.id), "tool_call_id", "call_"):
        assert internal not in json.dumps(body["data"])


def test_rows_are_isolated_per_user_and_conversation(client, login, agent, counted_tools):
    results, _ = counted_tools
    results["get_low_stock_items"] = low_stock(2)
    agent([tool_call("get_low_stock_items", {}), AIMessage(content="Two items."),
           AIMessage(content="Two items.")])
    _, first = login(Role.INVENTORY_MANAGER)
    _, second = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    assert send(client, first, conversation, "Show low stock items").json()["data"]["datasets"]

    # Same conversation id, different user: a different thread, so the first user's rows are not reachable.
    follow_up = send(client, second, conversation, "put that in a table").json()
    assert follow_up["data"] == {"datasets": [], "metrics": []}


def test_mutation_replies_never_carry_data(client, login, agent, layer8):  # noqa: F811
    agent([tool_call(MOVE, VALID[MOVE])])
    _, headers = login(Role.INVENTORY_MANAGER)
    body = send(client, headers, uuid.uuid4(), "Add 2 kg of BO-MC-0100 at 132-1").json()
    assert body["mutation"]["state"] == "confirmation_required"
    assert body["data"] == {"datasets": [], "metrics": []}
