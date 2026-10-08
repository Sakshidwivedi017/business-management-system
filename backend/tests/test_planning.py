"""Layer 7: intent classification and response planning. Pure functions: no model, database or user."""

import inspect
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.agent import graph as agent_graph
from app.agent import planning
from app.agent.graph import ITERATION_LIMIT_REPLY, Agent, AgentReply, thread_id_for
from app.agent.planning import (
    RESULT_SHAPES,
    ChartKind,
    Confidence,
    Intent,
    Presentation,
    ResponsePlan,
    Source,
    plan_turn,
)
from app.agent.prompts import response_guidance
from app.agent.tools import TOOLS, ToolResult
from app.auth.permissions import Role
from tests.fakes import ScriptedChatModel, make_agent_user, tool_call


def result(name, value, call_id=None, status="success"):
    return ToolMessage(
        content=json.dumps({"result": value}), tool_call_id=call_id or f"call_{name}", name=name, status=status
    )


def conversation(*parts):
    """Strings become user messages, each answered by the assistant; messages are kept as they are."""
    messages = []
    for part in parts:
        if isinstance(part, str):
            if messages and isinstance(messages[-1], HumanMessage):
                messages.append(AIMessage(content="ok"))
            messages.append(HumanMessage(content=part))
        else:
            messages.append(part)
    return messages


def plan(*parts) -> ResponsePlan:
    return plan_turn(conversation(*parts))


VENDORS = [{"id": "V1", "name": "Shakti Steels"}, {"id": "V2", "name": "Trident Steels"}, {"id": "V3", "name": "Zenith"}]
INVENTORY_SUMMARY = {
    "total_items": 501,
    "active_items": 480,
    "low_stock": {"count": 12, "fallback_threshold": "0"},
    "stock_by_location": [
        {"location_name": "132-1", "items_in_stock": 40, "items_out_of_stock": 3},
        {"location_name": "C-84", "items_in_stock": 25, "items_out_of_stock": 1},
    ],
    "stock_by_category": [{"category_name": "Bought Out", "active_items": 300, "items_in_stock": 200}],
    "recent_transactions": {"days": 30, "by_type": [{"transaction_type": "inbound", "transactions": 9}]},
}
TRANSACTIONS = [
    {"item_code": "BO-MC-0100", "quantity": 5, "created_at": "2026-09-01T10:00:00"},
    {"item_code": "BO-MC-0100", "quantity": -2, "created_at": "2026-09-05T10:00:00"},
    {"item_code": "BO-MC-0100", "quantity": 7, "created_at": "2026-09-09T10:00:00"},
]


# --- intent and source ----------------------------------------------------------


@pytest.mark.parametrize(
    "message, intent, source",
    [
        ("What is item BO-MC-0100?", Intent.INFORMATIONAL, Source.STRUCTURED_TOOL),
        ("Which vendors do we have?", Intent.INFORMATIONAL, Source.STRUCTURED_TOOL),
        ("Show me the details of PO-2026-0001", Intent.INFORMATIONAL, Source.STRUCTURED_TOOL),
        ("Update the stock for item BO-MC-0100", Intent.OPERATIONAL, Source.STRUCTURED_TOOL),
        ("Create a purchase order", Intent.OPERATIONAL, Source.STRUCTURED_TOOL),
        ("Can you receive 50 units of BO-MC-0100 at C-84?", Intent.OPERATIONAL, Source.STRUCTURED_TOOL),
        ("What is our total inventory?", Intent.ANALYTICAL, Source.STRUCTURED_TOOL),
        ("How many purchase orders were placed this month?", Intent.ANALYTICAL, Source.STRUCTURED_TOOL),
        ("Compare procurement activity", Intent.ANALYTICAL, Source.STRUCTURED_TOOL),
        ("What does this item classification mean?", Intent.KNOWLEDGE, Source.SEMANTIC_RETRIEVAL),
        ("Which category would this type of item belong to?", Intent.KNOWLEDGE, Source.SEMANTIC_RETRIEVAL),
        ("What naming convention should be used?", Intent.KNOWLEDGE, Source.SEMANTIC_RETRIEVAL),
        ("What is a chequered plate?", Intent.KNOWLEDGE, Source.SEMANTIC_RETRIEVAL),
        ("Do we have anything like an anti-skid plate in stock?", Intent.MIXED, Source.MIXED),
        ("hi", Intent.CONVERSATIONAL, Source.NONE),
        ("Thank you!", Intent.CONVERSATIONAL, Source.NONE),
        ("do it", Intent.UNCLEAR, Source.CLARIFICATION_REQUIRED),
        ("how many?", Intent.UNCLEAR, Source.CLARIFICATION_REQUIRED),
    ],
)
def test_intent_and_source(message, intent, source):
    result_plan = plan(message)
    assert (result_plan.intent, result_plan.source) == (intent, source)


@pytest.mark.parametrize(
    "message",
    [
        "Which vendors do we place most orders with?",  # a question about orders, not an order
        "How do I record a stock movement?",
        "Show me open purchase orders",
        "How many purchase orders were placed last month?",
    ],
)
def test_questions_about_changes_are_not_operational(message):
    assert plan(message).intent is not Intent.OPERATIONAL


def test_operational_dominates_and_is_never_presented_as_data():
    result_plan = plan("Check vendor V1 and create a purchase order for 10 BO-MC-0100", result("list_vendors", VENDORS))
    assert result_plan.intent is Intent.OPERATIONAL
    assert result_plan.presentation is Presentation.TEXT


def test_unrecognised_subject_is_still_a_request():
    # Item names are open-ended; a request verb is enough to plan a structured lookup.
    result_plan = plan("find bushes")
    assert (result_plan.intent, result_plan.confidence) == (Intent.INFORMATIONAL, Confidence.MEDIUM)
    assert not result_plan.clarification_required


# --- ambiguity ------------------------------------------------------------------------


def test_stock_without_item_or_location_needs_clarification():
    result_plan = plan("Show me the stock.")
    assert result_plan.clarification_required and result_plan.missing == ("item or location",)
    assert result_plan.presentation is Presentation.TEXT


@pytest.mark.parametrize(
    "messages",
    [
        ("What's in stock at C-84?",),
        ("Which items are low on stock?",),
        ("Tell me about BO-MC-0100", "Show me the stock"),  # the conversation names the item
        ("Show me the stock", "at 132-2"),  # the follow-up supplies it
    ],
)
def test_scoped_stock_questions_need_no_clarification(messages):
    assert not plan(*messages).clarification_required


def test_purchase_order_without_details_lists_what_is_missing():
    result_plan = plan("Create a purchase order.")
    assert result_plan.clarification_required
    assert result_plan.missing == ("vendor", "item", "quantity")


def test_stock_movement_without_details_lists_what_is_missing():
    result_plan = plan("Update the stock for item BO-MC-0100")
    assert result_plan.missing == ("quantity", "location", "direction")


RECEIPT_REASON = "asks to record goods received against a purchase order"
MOVEMENT_REASON = "asks to record a stock movement"


@pytest.mark.parametrize(
    "message",
    [
        "We received 6 pcs for PO-2026-0057 and 3 pcs for PO-2026-0056.",
        "Receive 6 pcs against PO-2026-0057 at 103-1",
        "Receiving 3 pcs on PO-2026-0056 at 103-1",
        "6 pcs of BO-MC-0172 arrived for PO-2026-0057",
        "The vendor delivered 10 units on purchase order PO-2026-0057",
        "Received 4 kg against the PO today at 132-1",
        # Found in the live smoke test: the noun was planned as a stock movement missing its direction.
        "Record a receipt of 1 pcs of BO-MC-0172 against PO-2026-0057 at 103-1",
        "Log a goods receipt for 6 pcs on PO-2026-0057 at 103-1",
    ],
)
def test_goods_received_against_a_purchase_order_is_a_receipt(message):
    result_plan = plan(message)
    assert result_plan.intent is Intent.OPERATIONAL and result_plan.reason == RECEIPT_REASON
    assert result_plan.presentation is Presentation.TEXT


@pytest.mark.parametrize(
    "message",
    [
        "Receive 50 units of BO-MC-0100 at C-84",  # no purchase order: an ordinary stock movement
        "Add 5 pcs of BO-MC-0100 at 132-1",
    ],
)
def test_stock_movements_are_not_reclassified_as_receipts(message):
    assert plan(message).reason == MOVEMENT_REASON


def test_received_without_a_purchase_order_is_never_a_receipt():
    assert plan("We received 6 pcs of BO-MC-0172 at 103-1").reason != RECEIPT_REASON


@pytest.mark.parametrize(
    "message",
    [
        "Has PO-2026-0057 been received?",
        "Which purchase orders were delivered in 2026?",
        "Show received purchase orders from 2026",
        "List POs that arrived last week",
        "How do I record a receipt for PO-2026-0057?",
        "Receipts for PO-2026-0057 from 2026",
    ],
)
def test_questions_about_receipts_are_not_operational(message):
    assert plan(message).intent is not Intent.OPERATIONAL


def test_receipt_lists_only_what_the_user_must_supply():
    assert plan("We received 6 pcs for PO-2026-0057").missing == ("location",)
    assert plan("Received 6 pcs against the PO at 103-1").missing == ("purchase order",)
    assert plan("Receive 6 pcs against PO-2026-0057 at 103-1").missing == ()
    # The follow-up supplies the location; the plan stays a receipt.
    follow_up = plan("We received 6 pcs for PO-2026-0057", "at 103-1")
    assert follow_up.reason.startswith("adds details") and follow_up.missing == ()


def test_details_supplied_in_follow_ups_complete_the_operation():
    result_plan = plan("Create a purchase order", "From Shakti Steels, 100 units of BO-MC-0100")
    assert result_plan.intent is Intent.OPERATIONAL and result_plan.confidence is Confidence.MEDIUM
    assert result_plan.missing == () and not result_plan.clarification_required


def test_unclear_requests_need_clarification_and_get_no_data():
    result_plan = plan("do it", result("list_vendors", VENDORS))
    assert result_plan.clarification_required and result_plan.source is Source.CLARIFICATION_REQUIRED
    assert result_plan.presentation is Presentation.TEXT


# --- follow-ups -------------------------------------------------------------------


def test_follow_ups_inherit_the_previous_request():
    first = result("get_low_stock_items", {"items": [{"item_code": "A"}, {"item_code": "B"}]}, "c1")
    narrowed = plan("Show me low-stock items.", first, "Only for 132-2.")
    assert (narrowed.intent, narrowed.source, narrowed.confidence) == (
        Intent.INFORMATIONAL, Source.STRUCTURED_TOOL, Confidence.MEDIUM
    )


def test_presentation_follow_up_reuses_the_previous_turns_data():
    first = result("get_low_stock_items", {"items": [{"item_code": "A"}, {"item_code": "B"}]}, "c1")
    tabled = plan("Show me low-stock items.", first, "Only for 132-2.", "Put that in a table.")
    # "Only for 132-2" had no tool result, so there is no data to put in a table: never fabricated.
    assert tabled.presentation is Presentation.TEXT and tabled.datasets == ()

    again = result("get_low_stock_items", {"items": [{"item_code": "A"}, {"item_code": "C"}]}, "c2")
    tabled = plan("Show me low-stock items.", first, "Only for 132-2.", again, "Put that in a table.")
    assert tabled.presentation is Presentation.TABLE and tabled.requested_presentation is Presentation.TABLE
    assert [d.tool_call_id for d in tabled.datasets] == ["c2"]


def test_a_new_subject_is_not_a_follow_up():
    assert plan("Which vendors do we have?", "What is a chequered plate?").intent is Intent.KNOWLEDGE
    assert plan("Show me low-stock items.", "thanks").intent is Intent.CONVERSATIONAL


def test_context_is_bounded():
    chatter = [f"Only for 132-{n}" for n in range(planning.CONTEXT_MESSAGES)]
    assert plan("Show me low-stock items.", *chatter).intent is Intent.UNCLEAR


# --- presentation ---------------------------------------------------------------------


def test_single_record_is_text():
    assert plan("Show vendor V1", result("get_vendor", VENDORS[0])).presentation is Presentation.TEXT


@pytest.mark.parametrize("rows, expected", [(VENDORS, Presentation.TABLE), (VENDORS[:1], Presentation.TEXT),
                                            ([], Presentation.TEXT)])
def test_lists_become_tables_only_with_several_rows(rows, expected):
    result_plan = plan("Which vendors do we have?", result("list_vendors", rows))
    assert result_plan.presentation is expected
    assert [d.rows for d in result_plan.datasets] == ([len(rows)] if rows else [])


def test_record_with_rows_is_mixed():
    details = {"item": {"item_code": "BO-MC-0100"}, "stock": [{"location_name": "132-1"}, {"location_name": "C-84"}]}
    result_plan = plan("Tell me about BO-MC-0100", result("get_item_details", details))
    assert result_plan.presentation is Presentation.MIXED
    [dataset] = result_plan.datasets
    assert (dataset.path, dataset.rows, dataset.chart) == ("stock", 2, None)


def test_kpis_are_a_summary():
    result_plan = plan("Give me an inventory overview", result("inventory_summary", INVENTORY_SUMMARY))
    assert result_plan.presentation is Presentation.SUMMARY
    assert all(d.chart is None for d in result_plan.datasets)


def test_single_aggregate_is_a_summary():
    orders = [{"po_number": "PO-2026-0001"}, {"po_number": "PO-2026-0002"}]
    result_plan = plan("How many purchase orders were placed this month?", result("list_purchase_orders", orders))
    assert result_plan.presentation is Presentation.SUMMARY


def test_comparison_of_categories_is_a_bar_chart_of_the_relevant_dataset():
    result_plan = plan("Compare stock by location", result("inventory_summary", INVENTORY_SUMMARY))
    assert result_plan.presentation is Presentation.CHART
    charted = {d.path: d.chart for d in result_plan.datasets}
    # stock_by_category has one row: it is not chartable at all.
    assert charted == {"stock_by_location": ChartKind.BAR, "stock_by_category": None,
                       "recent_transactions.by_type": None}
    by_location = next(d for d in result_plan.datasets if d.path == "stock_by_location")
    assert (by_location.x, by_location.y) == ("location_name", ("items_in_stock", "items_out_of_stock"))


def test_trend_over_time_is_a_line_chart():
    result_plan = plan("Stock trend for BO-MC-0100 over time", result("get_transaction_history", TRANSACTIONS))
    assert result_plan.presentation is Presentation.CHART
    [dataset] = result_plan.datasets
    assert (dataset.chart, dataset.x, dataset.series) == (ChartKind.LINE, "created_at", "item_code")


def test_numbers_alone_do_not_make_a_chart():
    orders = [{"po_number": "PO-2026-0001", "total_amount": "10.00"}, {"po_number": "PO-2026-0002",
                                                                         "total_amount": "20.00"}]
    assert plan("List purchase orders", result("list_purchase_orders", orders)).presentation is Presentation.TABLE
    # Trend wording without time-series data is still no chart.
    assert plan("Purchase order trend over time", result("list_purchase_orders", orders)).presentation is not (
        Presentation.CHART
    )


def test_currency_amounts_are_split_never_summed():
    summary = {"top_vendors_by_spend": [{"vendor_name": "A", "currency": "INR", "total_amount": "5"},
                                        {"vendor_name": "B", "currency": "USD", "total_amount": "7"}]}
    [dataset] = plan("Top vendors by spend", result("procurement_summary", summary)).datasets
    assert dataset.series == "currency" and dataset.chart is ChartKind.BAR


def test_knowledge_answers_are_text_without_datasets():
    hits = {"results": [{"title": "Chequered Plate"}, {"title": "Plain Plate"}], "min_similarity": 0.3}
    result_plan = plan("What is a chequered plate?", result("search_knowledge", hits))
    assert result_plan.presentation is Presentation.TEXT and result_plan.datasets == ()


def test_knowledge_with_live_rows_is_mixed():
    details = {"item": {"item_code": "RM-MT-FS-0001"}, "stock": [{"quantity": 1}, {"quantity": 2}]}
    result_plan = plan(
        "Do we have anything like an anti-skid plate in stock?",
        result("search_knowledge", {"results": [{"title": "Chequered Plate"}]}),
        result("get_item_details", details),
    )
    assert result_plan.presentation is Presentation.MIXED


def test_failed_or_unreadable_results_carry_no_data():
    errored = ToolMessage(content=json.dumps({"error": "nope"}), tool_call_id="e", name="list_vendors", status="error")
    garbled = ToolMessage(content="not json", tool_call_id="g", name="list_vendors")
    result_plan = plan("Which vendors do we have?", errored, garbled)
    assert result_plan.datasets == () and result_plan.presentation is Presentation.TEXT


# --- explicit presentation requests -------------------------------------------------


@pytest.mark.parametrize(
    "request_text, expected",
    [
        ("Show me the vendors in a table", Presentation.TABLE),
        ("Give me a chart of vendors", Presentation.CHART),
        ("Summarize the vendors", Presentation.SUMMARY),
        ("Show vendors as a table and a chart", Presentation.MIXED),
        ("Which vendors do we have?", None),
    ],
)
def test_explicit_requests_are_recorded(request_text, expected):
    assert plan(request_text).requested_presentation is expected


def test_explicit_table_needs_data():
    assert plan("Show this in a table").presentation is Presentation.TEXT
    assert plan("Vendors in a table please", result("list_vendors", VENDORS[:1])).presentation is Presentation.TABLE


def test_explicit_chart_depends_on_the_data():
    chartable = result("inventory_summary", INVENTORY_SUMMARY)
    assert plan("Give me a chart of the inventory summary", chartable).presentation is Presentation.CHART
    # Records can be tabled, not charted; with no data there is nothing to show.
    assert plan("Give me a chart of vendors", result("list_vendors", VENDORS)).presentation is Presentation.TABLE
    assert plan("Give me a chart of vendors").presentation is Presentation.TEXT


def test_explicit_summary_overrides_the_table_default():
    assert plan("Summarize our vendors", result("list_vendors", VENDORS)).presentation is Presentation.SUMMARY


def test_explicit_chart_on_follow_up():
    first = result("inventory_summary", INVENTORY_SUMMARY, "c1")
    result_plan = plan("Inventory overview", first, "give me a chart")
    assert result_plan.presentation is Presentation.CHART
    assert {d.tool_call_id for d in result_plan.datasets} == {"c1"}


# --- boundaries -----------------------------------------------------------------------


def test_plan_is_independent_of_user_role_and_permissions():
    assert list(inspect.signature(plan_turn).parameters) == ["messages"]
    source = inspect.getsource(planning)
    for forbidden in ("app.auth", "app.db", "app.services", "app.rag", "sqlalchemy", "openai", "Permission"):
        assert forbidden not in source
    serialized = plan("Show me the procurement summary").model_dump_json()
    assert "permission" not in serialized and "role" not in serialized


def test_every_tool_has_a_declared_result_shape_and_no_mutation_is_planned_for():
    assert set(RESULT_SHAPES) == set(TOOLS)
    assert not {"record_stock_movement", "create_purchase_order"} & set(RESULT_SHAPES)


def test_plans_are_deterministic_serialisable_and_immutable():
    messages = conversation("Compare stock by location", result("inventory_summary", INVENTORY_SUMMARY))
    first, second = plan_turn(messages), plan_turn(messages)
    assert first == second
    assert ResponsePlan.model_validate_json(first.model_dump_json()) == first
    json.loads(first.model_dump_json())  # plain JSON for a later API layer
    with pytest.raises(ValidationError):
        first.intent = Intent.UNCLEAR


def test_empty_conversation():
    result_plan = plan_turn([])
    assert result_plan.intent is Intent.UNCLEAR and result_plan.clarification_required


# --- guidance ---------------------------------------------------------------------------


def test_guidance_for_operational_requests_never_claims_a_change():
    # Layer 8: the change is proposed through its tool and confirmed by the user, never claimed by the model.
    guidance = response_guidance(plan("Create a purchase order"))
    assert "propose it" in guidance and "Never say a change was made" in guidance
    assert "It may still need: vendor, item, quantity." in guidance
    assert "clarifying" not in guidance


def test_guidance_for_a_role_without_change_tools_never_offers_one():
    # Found in the live smoke test: the owner offered to "propose" changes it has no tool for.
    guidance = response_guidance(plan("Add 5 pcs of BO-MC-0172 at 103-1"), can_change=False)
    assert "cannot make or propose changes" in guidance and "propose it once" not in guidance


def test_guidance_for_missing_scope():
    assert "which item or location" in response_guidance(plan("Show me the stock"))


def test_guidance_for_mixed_sources_and_requested_presentation():
    guidance = response_guidance(plan("Do we have anything like an anti-skid plate in stock? Show it in a table"))
    assert "search_knowledge" in guidance and "markdown table" in guidance


@pytest.mark.parametrize("message", ["hi", "do it", "Which vendors do we have?", "What is a chequered plate?"])
def test_no_guidance_when_the_plan_adds_nothing_reliable(message):
    assert response_guidance(plan(message)) is None


# --- agent integration --------------------------------------------------------------------


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def fake_execute(user, name, args):
        calls.append(name)
        rows = {"list_vendors": VENDORS}.get(name, [])
        return ToolResult(json.dumps({"result": rows}), is_error=False)

    monkeypatch.setattr(agent_graph, "execute_tool", fake_execute)
    return calls


def make_agent(responses, max_iterations=12):
    model = ScriptedChatModel(responses=responses)
    return Agent(model, InMemorySaver(), max_iterations), model


def test_respond_returns_text_and_plan(executed):
    agent, _ = make_agent([tool_call("list_vendors", {}, "v1"), AIMessage(content="Three vendors.")])
    reply = agent.respond(make_agent_user(Role.PROCUREMENT_MANAGER), uuid.uuid4(), "Which vendors do we have?")
    assert isinstance(reply, AgentReply) and reply.text == "Three vendors."
    assert (reply.plan.intent, reply.plan.presentation) == (Intent.INFORMATIONAL, Presentation.TABLE)
    assert [(d.tool, d.tool_call_id, d.rows) for d in reply.plan.datasets] == [("list_vendors", "v1", 3)]


def test_chat_still_returns_text(executed):
    agent, _ = make_agent([AIMessage(content="Hello!")])
    assert agent.chat(make_agent_user(Role.OWNER), uuid.uuid4(), "hi") == "Hello!"


def test_guidance_is_sent_at_turn_start_only_and_never_stored(executed):
    user = make_agent_user(Role.PROCUREMENT_MANAGER)
    agent, model = make_agent([tool_call("list_vendors", {}), AIMessage(content="I can't create orders.")])
    conversation_id = uuid.uuid4()
    agent.chat(user, conversation_id, "Create a purchase order")
    first, second = model.calls
    assert isinstance(first[-1], SystemMessage) and "Never say a change was made" in first[-1].content
    assert isinstance(first[0], SystemMessage) and "procurement manager" in first[0].content
    assert isinstance(second[-1], ToolMessage)  # no guidance after tool results
    stored = agent.graph.get_state({"configurable": {"thread_id": thread_id_for(user, conversation_id)}})
    assert not any(isinstance(m, SystemMessage) for m in stored.values["messages"])
    assert set(stored.values) == {"messages"}


def test_no_guidance_message_for_plain_requests(executed):
    agent, model = make_agent([AIMessage(content="Hello!")])
    agent.chat(make_agent_user(Role.OWNER), uuid.uuid4(), "hi")
    assert [type(m) for m in model.calls[0]] == [SystemMessage, HumanMessage]


@pytest.mark.parametrize("message", ["Create a purchase order for 10 BO-MC-0100", "Show me the procurement summary"])
def test_plan_never_changes_the_offered_tools(executed, message):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, model = make_agent([AIMessage(content="ok"), AIMessage(content="ok")])
    agent.chat(user, uuid.uuid4(), "hi")
    agent.chat(user, uuid.uuid4(), message)
    baseline, planned = model.bound_tools
    assert baseline == planned
    # Tools follow the role only: the inventory manager's own write tool, nothing beyond its permissions.
    assert "record_stock_movement" in planned
    assert not {"create_purchase_order", "procurement_summary"} & set(planned)


def test_unfinished_answer_is_presented_as_text(executed):
    agent, _ = make_agent([tool_call("list_vendors", {}) for _ in range(3)], max_iterations=2)
    reply = agent.respond(make_agent_user(Role.OWNER), uuid.uuid4(), "Which vendors do we have?")
    assert reply.text == ITERATION_LIMIT_REPLY and reply.plan.presentation is Presentation.TEXT
