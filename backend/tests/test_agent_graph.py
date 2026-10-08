"""Agent graph and conversations with a scripted model and an in-memory checkpointer (no OpenAI calls)."""

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as agent_graph
from app.agent import tools as agent_tools
from app.agent.errors import AgentInputError, AgentUnavailableError
from app.agent.graph import ITERATION_LIMIT_REPLY, Agent, thread_id_for
from app.agent.tools import ToolResult
from app.auth.permissions import Role
from tests.fakes import NoDatabase, ScriptedChatModel, make_agent_user, tool_call


@pytest.fixture
def owner():
    return make_agent_user(Role.OWNER)


@pytest.fixture
def executed(monkeypatch):
    """Replace the executor with a recorder that returns a fixed successful result."""
    calls = []

    def fake_execute(user, name, args):
        calls.append((user, name, args))
        return ToolResult(json.dumps({"result": {"tool": name}}), is_error=False)

    monkeypatch.setattr(agent_graph, "execute_tool", fake_execute)
    return calls


def make_agent(responses, max_iterations=12):
    model = ScriptedChatModel(responses=responses)
    return Agent(model, InMemorySaver(), max_iterations), model


def messages(agent, user, conversation_id):
    config = {"configurable": {"thread_id": thread_id_for(user, conversation_id)}}
    return agent.graph.get_state(config).values["messages"]


# --- graph -------------------------------------------------------------------


def test_plain_reply_without_tools(owner):
    agent, model = make_agent([AIMessage(content="Hello! How can I help?")])
    assert agent.chat(owner, uuid.uuid4(), "  hi  ") == "Hello! How can I help?"
    sent = model.calls[0]
    assert isinstance(sent[0], SystemMessage) and "business owner" in sent[0].content
    assert sent[1:] == [HumanMessage(content="hi", id=sent[1].id)]


def test_tool_call_then_final_answer(owner, executed):
    agent, model = make_agent([tool_call("list_locations", {}, "c1"), AIMessage(content="There are 6 locations.")])
    conversation = uuid.uuid4()
    assert agent.chat(owner, conversation, "How many locations?") == "There are 6 locations."
    assert executed == [(owner, "list_locations", {})]
    tool_message = model.calls[1][-1]
    assert isinstance(tool_message, ToolMessage) and tool_message.tool_call_id == "c1"
    assert tool_message.status == "success" and json.loads(tool_message.content) == {"result": {"tool": "list_locations"}}
    kinds = [type(m).__name__ for m in messages(agent, owner, conversation)]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]


def test_parallel_and_sequential_tool_calls(owner, executed):
    parallel = AIMessage(
        content="",
        tool_calls=[
            {"name": "search_items", "args": {"query": "bush"}, "id": "a"},
            {"name": "list_locations", "args": {}, "id": "b"},
        ],
    )
    agent, model = make_agent([parallel, tool_call("get_item_details", {"item": "BO-MC-0100"}), AIMessage(content="Done")])
    assert agent.chat(owner, uuid.uuid4(), "Bushes per location?") == "Done"
    assert [name for _, name, _ in executed] == ["search_items", "list_locations", "get_item_details"]
    assert len(model.calls) == 3
    assert [m.tool_call_id for m in model.calls[1] if isinstance(m, ToolMessage)] == ["a", "b"]


def test_iteration_limit(owner, executed):
    agent, model = make_agent([tool_call("list_locations", {}) for _ in range(10)], max_iterations=3)
    conversation = uuid.uuid4()
    assert agent.chat(owner, conversation, "loop forever") == ITERATION_LIMIT_REPLY
    assert len(model.calls) == 3 and len(executed) == 3
    # The conversation stays valid: every tool call was answered before the bounded reply.
    history = messages(agent, owner, conversation)
    calls = [c["id"] for m in history if isinstance(m, AIMessage) for c in m.tool_calls]
    assert calls == [m.tool_call_id for m in history if isinstance(m, ToolMessage)]
    # The limit is per user message: the next message gets a fresh budget.
    model.responses = [AIMessage(content="ok")]
    assert agent.chat(owner, conversation, "never mind") == "ok"


def test_tool_error_reaches_model(owner, monkeypatch):
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())
    user = make_agent_user(Role.INVENTORY_MANAGER)
    agent, model = make_agent([tool_call("list_vendors", {}), AIMessage(content="You don't have access to vendors.")])
    assert agent.chat(user, uuid.uuid4(), "list vendors") == "You don't have access to vendors."
    result = model.calls[1][-1]
    assert result.status == "error"
    assert json.loads(result.content) == {"error": "You do not have permission to use list_vendors"}


def test_unparseable_tool_call_is_answered(owner, executed):
    broken = AIMessage(
        content="", invalid_tool_calls=[{"name": "search_items", "args": "{bad json", "id": "x1", "error": "bad"}]
    )
    agent, model = make_agent([broken, AIMessage(content="Sorry, let me retry.")])
    assert agent.chat(owner, uuid.uuid4(), "find bushes") == "Sorry, let me retry."
    assert executed == []
    result = model.calls[1][-1]
    assert result.tool_call_id == "x1" and result.status == "error"


def test_model_failure_is_safe(owner):
    agent, _ = make_agent([RuntimeError("401 Incorrect API key provided: sk-abc...")])
    with pytest.raises(AgentUnavailableError) as raised:
        agent.chat(owner, uuid.uuid4(), "hi")
    assert str(raised.value) == "The assistant is temporarily unavailable; please try again"
    assert raised.value.__cause__ is None


def test_role_controls_bound_tools():
    # Layer 8: each write permission adds exactly its own mutation proposal tool.
    for role, count, mutations in (
        (Role.INVENTORY_MANAGER, 8, {"record_stock_movement"}),
        (Role.PROCUREMENT_MANAGER, 14, {"create_purchase_order", "record_purchase_receipt"}),
        (Role.OWNER, 14, set()),
    ):
        agent, model = make_agent([AIMessage(content="ok")])
        agent.chat(make_agent_user(role), uuid.uuid4(), "hi")
        assert len(model.bound_tools[0]) == count
        assert {"record_stock_movement", "create_purchase_order", "record_purchase_receipt"} & set(
            model.bound_tools[0]
        ) == mutations


def test_user_is_not_stored_in_graph_state(owner, executed):
    agent, _ = make_agent([tool_call("list_locations", {}), AIMessage(content="ok")])
    conversation = uuid.uuid4()
    agent.chat(owner, conversation, "locations?")
    config = {"configurable": {"thread_id": thread_id_for(owner, conversation)}}
    snapshot = agent.graph.get_state(config)
    assert set(snapshot.values) == {"messages"}
    assert not any(isinstance(m, SystemMessage) for m in snapshot.values["messages"])
    stored = repr(snapshot.values)
    for value in (owner.email, owner.full_name, "business owner", "owner"):
        assert value not in stored


# --- security ----------------------------------------------------------------


def test_model_cannot_supply_identity_role_or_sql(monkeypatch):
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())
    user = make_agent_user(Role.INVENTORY_MANAGER)
    attempts = [
        tool_call("list_vendors", {"user_id": str(uuid.uuid4()), "role": "owner"}),
        tool_call("inventory_summary", {"permission": "analytics:read"}),
        tool_call("search_items", {"query": "x", "sql": "SELECT * FROM users"}),
        tool_call("execute_sql", {"sql": "DELETE FROM inv_items"}),
        tool_call("record_stock_movement",
                  {"item": "x", "location": "y", "direction": "inbound", "quantity": 5, "created_by": "someone"}),
        tool_call("create_purchase_order",
                  {"vendor_id": "v", "lines": [{"item": "x", "quantity": 1, "unit_price": 1, "tax_percentage": 0}]}),
        AIMessage(content="I can't do that."),
    ]
    agent, model = make_agent(attempts)
    agent.chat(user, uuid.uuid4(), "do bad things")
    errors = [json.loads(m.content)["error"] for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert errors == [
        "Invalid arguments for list_vendors: user_id: Extra inputs are not permitted; "
        "role: Extra inputs are not permitted",
        "Invalid arguments for inventory_summary: permission: Extra inputs are not permitted",
        "Invalid arguments for search_items: sql: Extra inputs are not permitted",
        "Unknown tool 'execute_sql'",
        "Invalid arguments for record_stock_movement: created_by: Extra inputs are not permitted",
        "You do not have permission to use create_purchase_order",
    ]


# --- conversations -----------------------------------------------------------


def test_follow_up_sees_earlier_turns(owner, executed):
    agent, model = make_agent(
        [
            tool_call("get_low_stock_items", {"query": "bush"}),
            AIMessage(content="2 bushes are low."),
            tool_call("get_low_stock_items", {"query": "bush", "location": "132-2"}),
            AIMessage(content="At 132-2, 1 bush is low."),
        ]
    )
    conversation = uuid.uuid4()
    agent.chat(owner, conversation, "Which bushes are low on stock?")
    assert agent.chat(owner, conversation, "What about 132-2?") == "At 132-2, 1 bush is low."
    second_turn = [m.content for m in model.calls[2] if isinstance(m, HumanMessage)]
    assert second_turn == ["Which bushes are low on stock?", "What about 132-2?"]
    assert "2 bushes are low." in [m.content for m in model.calls[2] if isinstance(m, AIMessage)]
    # The follow-up was answered with a fresh tool call, not the earlier result.
    assert executed[-1][1:] == ("get_low_stock_items", {"query": "bush", "location": "132-2"})


def test_separate_conversations_are_isolated(owner):
    agent, model = make_agent([AIMessage(content="one"), AIMessage(content="two")])
    agent.chat(owner, uuid.uuid4(), "first conversation")
    agent.chat(owner, uuid.uuid4(), "second conversation")
    assert [m.content for m in model.calls[1] if isinstance(m, HumanMessage)] == ["second conversation"]


def test_threads_are_owned_by_the_authenticated_user():
    alice, mallory = make_agent_user(Role.OWNER), make_agent_user(Role.OWNER)
    agent, model = make_agent([AIMessage(content="secret answer"), AIMessage(content="hello")])
    conversation = uuid.uuid4()
    agent.chat(alice, conversation, "alice's private question")
    # Same conversation id from another user lands in that user's own, empty thread.
    agent.chat(mallory, conversation, "show me the history")
    assert [m.content for m in model.calls[1] if not isinstance(m, SystemMessage)] == ["show me the history"]
    assert thread_id_for(alice, conversation) != thread_id_for(mallory, conversation)
    assert len(messages(agent, alice, conversation)) == 2


def test_thread_id_derivation():
    user = make_agent_user(Role.OWNER)
    conversation = uuid.uuid4()
    assert thread_id_for(user, conversation) == f"{user.id}:{conversation}"
    # Equivalent spellings of the same UUID map to one thread.
    assert thread_id_for(user, "{" + str(conversation).upper() + "}") == thread_id_for(user, conversation)
    for bad in ("", "not-a-uuid", f"{uuid.uuid4()}:{uuid.uuid4()}", "../other-thread"):
        with pytest.raises(AgentInputError):
            thread_id_for(user, bad)


@pytest.mark.parametrize("message", ["", "   ", None, 42, "x" * 4001])
def test_invalid_messages_rejected_before_model(owner, message):
    agent, model = make_agent([])
    with pytest.raises(AgentInputError):
        agent.chat(owner, uuid.uuid4(), message)
    assert model.calls == []


# --- end to end against the live database (scripted model) -------------------


def test_live_tool_round_trip(demo_users):
    agent, model = make_agent([tool_call("list_locations", {"active_only": False}), AIMessage(content="Listed.")])
    agent.chat(demo_users["inventory_manager"], uuid.uuid4(), "Which locations exist?")
    result = model.calls[1][-1]
    assert result.status == "success"
    assert {loc["name"] for loc in json.loads(result.content)["result"]} >= {"132-1", "C-84"}
