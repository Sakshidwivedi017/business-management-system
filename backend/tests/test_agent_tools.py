"""Agent tool registry, argument schemas, role filtering and the executor."""

import dataclasses
import datetime
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.agent import tools as agent_tools
from app.agent.tools import TOOLS, available_tools, execute_tool
from app.auth.permissions import ROLE_PERMISSIONS, Permission, PermissionDenied, Role
from app.rag import search as knowledge
from app.services import NotFoundError, ValidationError, analytics, dashboard, inventory, procurement
from tests.fakes import NoDatabase, make_agent_user

# The smallest valid arguments for each tool.
MINIMAL_ARGS = {
    "search_items": {"query": "bush"},
    "get_item_details": {"item": "BO-MC-0100"},
    "list_locations": {},
    "get_stock_by_location": {"location": "132-1"},
    "get_transaction_history": {},
    "get_low_stock_items": {},
    "list_vendors": {},
    "get_vendor": {"vendor_id": "v1"},
    "list_purchase_orders": {},
    "get_purchase_order": {"purchase_order": "PO-2026-0001"},
    "inventory_summary": {},
    "procurement_summary": {},
    "procurement_overview": {},
    "search_knowledge": {"query": "anti-skid plate"},
}

EXPECTED = {
    "search_items": (Permission.INVENTORY_READ, inventory.search_items),
    "get_item_details": (Permission.INVENTORY_READ, inventory.get_item_details),
    "list_locations": (Permission.INVENTORY_READ, inventory.list_locations),
    "get_stock_by_location": (Permission.INVENTORY_READ, inventory.get_stock_by_location),
    "get_transaction_history": (Permission.INVENTORY_READ, inventory.get_transaction_history),
    "get_low_stock_items": (Permission.INVENTORY_READ, inventory.get_low_stock_items),
    "list_vendors": (Permission.PROCUREMENT_READ, procurement.list_vendors),
    "get_vendor": (Permission.PROCUREMENT_READ, procurement.get_vendor),
    "list_purchase_orders": (Permission.PROCUREMENT_READ, procurement.list_purchase_orders),
    "get_purchase_order": (Permission.PROCUREMENT_READ, procurement.get_purchase_order),
    "inventory_summary": (Permission.ANALYTICS_READ, analytics.inventory_summary),
    "procurement_summary": (Permission.ANALYTICS_READ, analytics.procurement_summary),
    # The procurement dashboard's own figures (status counts), behind the same procurement:read permission.
    "procurement_overview": (Permission.PROCUREMENT_READ, dashboard.procurement_overview),
    "search_knowledge": (Permission.INVENTORY_READ, knowledge.search_knowledge),
}

INVENTORY_TOOLS = {n for n, (p, _) in EXPECTED.items() if p is Permission.INVENTORY_READ}
PROCUREMENT_TOOLS = {n for n, (p, _) in EXPECTED.items() if p is Permission.PROCUREMENT_READ}
ANALYTICS_TOOLS = {n for n, (p, _) in EXPECTED.items() if p is Permission.ANALYTICS_READ}


@pytest.fixture
def no_database(monkeypatch):
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())


def replace_operation(monkeypatch, name, operation):
    monkeypatch.setitem(TOOLS, name, dataclasses.replace(TOOLS[name], operation=operation))


class FakeEngine:
    """Hands out a stub connection; for executor tests that replace the operation."""

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execution_options(self, **options):
        self.options = options
        return self


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(agent_tools, "get_engine", lambda: engine)
    return engine


def parse(result):
    return json.loads(result.content)


# --- registry ----------------------------------------------------------------


def test_registry_exposes_exactly_the_read_tools():
    assert set(TOOLS) == set(EXPECTED)
    for name, tool in TOOLS.items():
        assert tool.name == name
        assert (tool.permission, tool.operation) == EXPECTED[name]
        assert tool.mutation is False
        assert tool.description


def test_mutation_operations_are_not_tools():
    exposed_operations = {tool.operation for tool in TOOLS.values()}
    assert inventory.record_stock_movement not in exposed_operations
    assert procurement.create_purchase_order not in exposed_operations
    assert procurement.record_purchase_receipt not in exposed_operations
    assert not {"record_stock_movement", "create_purchase_order", "record_purchase_receipt"} & set(TOOLS)
    assert not any(p in (Permission.INVENTORY_WRITE, Permission.PROCUREMENT_WRITE) for p, _ in EXPECTED.values())


@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize("role", list(Role))
def test_tool_permission_matches_layer4_enforcement(name, role):
    # Where the registry says a role lacks the permission, Layer 4 itself refuses (before any DB access).
    tool = TOOLS[name]
    if tool.permission in ROLE_PERMISSIONS[role]:
        return
    with pytest.raises(PermissionDenied):
        tool.operation(None, make_agent_user(role), **MINIMAL_ARGS[name])


def test_openai_schemas_are_strict_and_have_no_identity_fields():
    for tool in TOOLS.values():
        schema = tool.openai_schema()
        assert schema["type"] == "function" and schema["function"]["name"] == tool.name
        params = schema["function"]["parameters"]
        assert params["additionalProperties"] is False
        assert not {"user", "user_id", "role", "permission", "permissions", "sql", "conn"} & set(params["properties"])


# --- role filtering ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        # Layer 8: write permissions add exactly their mutation proposal tool; the owner has none.
        (Role.INVENTORY_MANAGER, INVENTORY_TOOLS | {"record_stock_movement"}),
        (Role.PROCUREMENT_MANAGER,
         INVENTORY_TOOLS | PROCUREMENT_TOOLS | {"create_purchase_order", "record_purchase_receipt"}),
        (Role.OWNER, INVENTORY_TOOLS | PROCUREMENT_TOOLS | ANALYTICS_TOOLS),
    ],
)
def test_available_tools_by_role(role, expected):
    assert {tool.name for tool in available_tools(make_agent_user(role))} == expected


def test_mutation_flagged_tool_is_never_offered_or_run(monkeypatch, no_database):
    monkeypatch.setitem(TOOLS, "search_items", dataclasses.replace(TOOLS["search_items"], mutation=True))
    user = make_agent_user(Role.OWNER)
    assert "search_items" not in {tool.name for tool in available_tools(user)}
    assert parse(execute_tool(user, "search_items", {"query": "x"})) == {"error": "Unknown tool 'search_items'"}


# --- argument schemas --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_minimal_arguments_accepted(name):
    TOOLS[name].args_schema.model_validate(MINIMAL_ARGS[name])


@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize("field", ["user_id", "role", "permission", "sql", "conn"])
def test_unexpected_arguments_rejected(name, field, no_database):
    result = execute_tool(make_agent_user(Role.OWNER), name, {**MINIMAL_ARGS[name], field: "x"})
    assert result.is_error
    assert parse(result)["error"] == f"Invalid arguments for {name}: {field}: Extra inputs are not permitted"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_items", {}),
        ("search_items", {"query": ""}),
        ("search_items", {"query": "x" * 101}),
        ("search_items", {"query": "x", "limit": 0}),
        ("search_items", {"query": "x", "limit": 51}),
        ("search_items", {"query": "x", "limit": "5"}),  # strict: no string-to-int coercion
        ("search_items", {"query": 5}),
        ("get_low_stock_items", {"threshold": -1}),
        ("get_low_stock_items", {"threshold": "lots"}),
        ("list_locations", {"active_only": "yes"}),
        ("list_purchase_orders", {"status": "cancelled"}),
        ("inventory_summary", {"recent_days": 366}),
        ("search_items", None),
        ("search_items", ["query", "x"]),
    ],
)
def test_invalid_arguments_rejected(name, args, no_database):
    result = execute_tool(make_agent_user(Role.OWNER), name, args)
    assert result.is_error
    assert parse(result)["error"].startswith(f"Invalid arguments for {name}:")


def test_invalid_argument_values_are_not_echoed(no_database):
    payload = "'; DROP TABLE users; --" * 10
    result = execute_tool(make_agent_user(Role.OWNER), "search_items", {"query": payload})
    assert result.is_error and "DROP TABLE" not in result.content


# --- executor ----------------------------------------------------------------


# record_stock_movement and create_purchase_order are Layer 8 proposal tools now (tests/test_mutations.py).
@pytest.mark.parametrize("name", ["execute_sql", "delete_item", "update_vendor", "", None, 42])
def test_unknown_tool(name, no_database):
    result = execute_tool(make_agent_user(Role.OWNER), name, {})
    assert result.is_error and parse(result)["error"].startswith("Unknown tool")


def test_unauthorized_tool_denied_before_database(no_database):
    result = execute_tool(make_agent_user(Role.INVENTORY_MANAGER), "list_vendors", {})
    assert result.is_error
    assert parse(result) == {"error": "You do not have permission to use list_vendors"}


def test_layer4_remains_final_authority(monkeypatch, fake_engine):
    # Simulate a tool accidentally exposed by Layer 5: Layer 4 still refuses.
    monkeypatch.setattr(agent_tools, "has_permission", lambda user, permission: True)
    result = execute_tool(make_agent_user(Role.INVENTORY_MANAGER), "list_vendors", {})
    assert parse(result) == {"error": "You do not have permission to use list_vendors"}


def test_dispatch_passes_validated_arguments_and_authenticated_user(monkeypatch, fake_engine):
    seen = {}

    def operation(conn, user, **kwargs):
        seen.update(conn=conn, user=user, kwargs=kwargs)
        return [{"id": "i1"}]

    replace_operation(monkeypatch, "search_items", operation)
    user = make_agent_user(Role.INVENTORY_MANAGER)
    result = execute_tool(user, "search_items", {"query": "bush"})
    assert not result.is_error and parse(result) == {"result": [{"id": "i1"}]}
    assert seen == {"conn": fake_engine, "user": user, "kwargs": {"query": "bush", "limit": 20}}
    assert fake_engine.options == {"postgresql_readonly": True}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (NotFoundError("Item 'X' not found"), "Item 'X' not found"),
        (ValidationError("limit must be an integer between 1 and 200"), "limit must be an integer between 1 and 200"),
    ],
)
def test_business_errors_are_mapped(monkeypatch, fake_engine, error, message):
    def operation(conn, user, **kwargs):
        raise error

    replace_operation(monkeypatch, "get_item_details", operation)
    result = execute_tool(make_agent_user(Role.OWNER), "get_item_details", {"item": "X"})
    assert result.is_error and parse(result) == {"error": message}


def test_database_failure_is_sanitised(monkeypatch, fake_engine):
    def operation(conn, user, **kwargs):
        raise OperationalError("SELECT * FROM users", {"pw": "hunter2"}, Exception("postgresql://admin:hunter2@db"))

    replace_operation(monkeypatch, "search_items", operation)
    result = execute_tool(make_agent_user(Role.OWNER), "search_items", {"query": "x"})
    assert parse(result) == {"error": "Business data is temporarily unavailable; please try again later"}
    for leaked in ("SELECT", "hunter2", "postgresql://", "users"):
        assert leaked not in result.content


def test_connection_failure_is_sanitised(monkeypatch):
    class DownEngine:
        def connect(self):
            raise OperationalError("connect", {}, Exception("could not connect to server at 10.0.0.5"))

    monkeypatch.setattr(agent_tools, "get_engine", lambda: DownEngine())
    result = execute_tool(make_agent_user(Role.OWNER), "list_locations", {})
    assert parse(result) == {"error": "Business data is temporarily unavailable; please try again later"}


def test_unexpected_failure_is_generic(monkeypatch, fake_engine):
    def operation(conn, user, **kwargs):
        raise RuntimeError("/Users/secret/path.py line 3: boom")

    replace_operation(monkeypatch, "search_items", operation)
    result = execute_tool(make_agent_user(Role.OWNER), "search_items", {"query": "x"})
    assert parse(result) == {"error": "The tool failed unexpectedly"}


def test_safe_serialisation(monkeypatch, fake_engine):
    ident = uuid.uuid4()

    def operation(conn, user, **kwargs):
        return {
            "qty": Decimal("102.50"),
            "at": datetime.datetime(2026, 9, 22, 7, 14, 59),
            "on": datetime.date(2026, 9, 22),
            "id": ident,
            "name": "MS Bush – OD40×ID26.5",
        }

    replace_operation(monkeypatch, "get_item_details", operation)
    result = execute_tool(make_agent_user(Role.OWNER), "get_item_details", {"item": "X"})
    assert parse(result) == {
        "result": {
            "qty": "102.50",
            "at": "2026-09-22 07:14:59",
            "on": "2026-09-22",
            "id": str(ident),
            "name": "MS Bush – OD40×ID26.5",
        }
    }


def test_long_error_messages_are_bounded(monkeypatch, fake_engine):
    def operation(conn, user, **kwargs):
        raise NotFoundError("x" * 5000)

    replace_operation(monkeypatch, "get_item_details", operation)
    assert len(parse(execute_tool(make_agent_user(Role.OWNER), "get_item_details", {"item": "X"}))["error"]) == 300


# --- live database -----------------------------------------------------------


def test_live_read_tool(demo_users):
    result = execute_tool(demo_users["inventory_manager"], "list_locations", {"active_only": False})
    assert not result.is_error
    locations = parse(result)["result"]
    assert locations and {"id", "name", "address", "is_active"} == set(locations[0])


def test_live_procurement_overview_for_the_procurement_manager(demo_users):
    result = execute_tool(demo_users["procurement_manager"], "procurement_overview", {})
    assert not result.is_error
    overview = parse(result)["result"]
    assert set(overview) == {"total_vendors", "active_vendors", "purchase_orders_by_status", "open_purchase_orders"}
    assert "top_vendors_by_spend" not in overview  # vendor spend stays an owner (analytics) figure
    denied = execute_tool(demo_users["inventory_manager"], "procurement_overview", {})
    assert parse(denied) == {"error": "You do not have permission to use procurement_overview"}


def test_live_business_error(demo_users):
    result = execute_tool(demo_users["owner"], "get_item_details", {"item": "NO-SUCH-ITEM"})
    assert parse(result) == {"error": "Item 'NO-SUCH-ITEM' not found"}


def test_live_connection_is_read_only(monkeypatch, demo_users):
    # Even a wrongly registered write cannot change data: the tool transaction is read-only.
    def write(conn, user, **kwargs):
        conn.execute(text("UPDATE inv_locations SET name = name WHERE false"))

    replace_operation(monkeypatch, "list_locations", write)
    result = execute_tool(demo_users["owner"], "list_locations", {})
    assert parse(result) == {"error": "Business data is temporarily unavailable; please try again later"}
