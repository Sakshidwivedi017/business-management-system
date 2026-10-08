"""Layer 9: POST /api/chat over the real auth dependency and the real agent graph.

The model is scripted and conversations use an in-memory checkpointer, so no OpenAI call
is made. User lookups are in memory except in the live tests at the end.
"""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import AgentConfigError, AgentError, AgentInputError, AgentUnavailableError, plan_turn
from app.agent import graph as agent_graph
from app.agent import mutations
from app.agent import tools as agent_tools
from app.agent.graph import ITERATION_LIMIT_REPLY, MAX_MESSAGE_LENGTH, Agent, AgentReply, thread_id_for
from app.agent.mutations import MutationState
from app.api import chat as chat_api
from app.api.chat import ChatResponse, ConversationBusy, ConversationLocks, get_agent
from app.auth import dependencies
from app.auth.dependencies import to_authenticated_user
from app.auth.permissions import Permission, PermissionDenied, Role
from app.auth.security import create_access_token
from app.db.connection import get_engine
from app.db.repositories.users import get_user_by_id
from app.main import app
from app.services import BusinessError
from app.services.errors import ConflictError, InsufficientStockError, NotFoundError, OperationFailedError, ValidationError
from tests.conftest import assert_no_sensitive_data, make_user
from tests.fakes import NoDatabase, ScriptedChatModel, tool_call
from tests.test_mutations import MOVE, ORDER, VALID, db, inv_user, layer8, move, stock, stocked  # noqa: F401 (fixtures)

URL = "/api/chat"


@pytest.fixture
def auth_engine(monkeypatch):
    """Stands in for the engine behind get_current_user_released and tracks whether its connection is open."""

    class Engine:
        open = 0

        @contextmanager
        def connect(self):
            self.open += 1
            try:
                yield None  # user lookups are faked by fake_users
            finally:
                self.open -= 1

    engine = Engine()
    monkeypatch.setattr(dependencies, "get_engine", lambda: engine)
    return engine


@pytest.fixture
def login(client, fake_users, auth_engine):
    """login(role) -> (AuthenticatedUser, headers) for a user known only to the in-memory store."""

    def make(role: Role):
        row = fake_users(make_user(role.value))
        token = create_access_token(row["id"], row["token_version"])[0]
        return to_authenticated_user(row), {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def agent():
    """install(responses) puts a real Agent with a scripted model behind the endpoint."""
    installed = {}

    def install(responses, max_iterations=12):
        model = ScriptedChatModel(responses=responses)
        installed["agent"] = Agent(model, InMemorySaver(), max_iterations)
        app.dependency_overrides[get_agent] = lambda: installed["agent"]
        return installed["agent"], model

    yield install
    app.dependency_overrides.pop(get_agent, None)


@pytest.fixture
def replies(monkeypatch):
    """Records every AgentReply the endpoint receives from Agent.respond."""
    seen = []
    original = Agent.respond

    def spy(self, user, conversation_id, message):
        seen.append((user, conversation_id, message))
        reply = original(self, user, conversation_id, message)
        seen.append(reply)
        return reply

    monkeypatch.setattr(Agent, "respond", spy)
    return seen


@pytest.fixture
def executed(monkeypatch):
    """Read tools answered by a fixed per-tool result (no database)."""
    results = {}

    def fake_execute(user, name, args, **kwargs):
        return agent_tools.ToolResult(json.dumps({"result": results.get(name, {"tool": name})}), is_error=False)

    monkeypatch.setattr(agent_graph, "execute_tool", fake_execute)
    return results


def send(client, headers, conversation, message):
    return client.post(URL, headers=headers, json={"conversation_id": str(conversation), "message": message})


def history(agent, user, conversation):
    config = {"configurable": {"thread_id": thread_id_for(user, conversation)}}
    return agent.graph.get_state(config).values.get("messages", [])


# --- request validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hi"},
        {"conversation_id": "not-a-uuid", "message": "hi"},
        {"conversation_id": "", "message": "hi"},
        {"conversation_id": str(uuid.uuid4())},
        {"conversation_id": str(uuid.uuid4()), "message": ""},
        {"conversation_id": str(uuid.uuid4()), "message": "   \n\t "},
        {"conversation_id": str(uuid.uuid4()), "message": "x" * (MAX_MESSAGE_LENGTH + 1)},
        {"conversation_id": str(uuid.uuid4()), "message": 42},
        {"conversation_id": str(uuid.uuid4()), "message": ["hi"]},
        # Identity, role and thread are never accepted from the client.
        {"conversation_id": str(uuid.uuid4()), "message": "hi", "user_id": str(uuid.uuid4())},
        {"conversation_id": str(uuid.uuid4()), "message": "hi", "role": "owner"},
        {"conversation_id": str(uuid.uuid4()), "message": "hi", "permissions": ["procurement:write"]},
        {"conversation_id": str(uuid.uuid4()), "message": "hi", "thread_id": "someone:else"},
    ],
)
def test_invalid_requests_rejected_before_the_model(client, login, agent, body):
    _, model = agent([])
    _, headers = login(Role.OWNER)
    response = client.post(URL, headers=headers, json=body)
    assert response.status_code == 422
    assert model.calls == []
    assert "Traceback" not in response.text


def test_non_json_body_rejected(client, login, agent):
    _, model = agent([])
    _, headers = login(Role.OWNER)
    response = client.post(URL, headers=headers, content=b"conversation_id=1")
    assert response.status_code == 422 and model.calls == []


def test_valid_request_message_is_trimmed(client, login, agent):
    _, model = agent([AIMessage(content="Hello!")])
    _, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    response = send(client, headers, conversation, "   hi there  \n")
    assert response.status_code == 200
    assert response.json()["message"] == "Hello!"
    assert [m.content for m in model.calls[0] if isinstance(m, HumanMessage)] == ["hi there"]


def test_longest_allowed_message_is_accepted(client, login, agent):
    agent([AIMessage(content="ok")])
    _, headers = login(Role.OWNER)
    assert send(client, headers, uuid.uuid4(), "x" * MAX_MESSAGE_LENGTH).status_code == 200


# --- authentication ------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic abc"}])
def test_unauthenticated_requests_rejected(client, fake_users, auth_engine, agent, headers):
    _, model = agent([])
    response = send(client, headers, uuid.uuid4(), "hi")
    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}
    assert model.calls == []


def test_revoked_token_rejected(client, fake_users, auth_engine, agent):
    _, model = agent([])
    row = fake_users(make_user("owner"))
    token = create_access_token(row["id"], row["token_version"])[0]
    row["token_version"] += 1  # e.g. logged out everywhere
    response = send(client, {"Authorization": f"Bearer {token}"}, uuid.uuid4(), "hi")
    assert response.status_code == 401 and model.calls == []


def test_authenticated_identity_and_role_reach_the_agent(client, login, agent, replies):
    _, model = agent([AIMessage(content="ok")])
    user, headers = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    assert send(client, headers, conversation, "hi").status_code == 200
    assert replies[0] == (user, conversation, "hi")
    # Tools bound for the token's role: inventory reads plus its one mutation proposal tool.
    assert len(model.bound_tools[0]) == 8 and "record_stock_movement" in model.bound_tools[0]
    assert "inventory manager" in model.calls[0][0].content


def test_auth_connection_is_released_before_the_agent_runs(client, login, auth_engine, monkeypatch):
    seen = []

    class Probe:
        def respond(self, user, conversation_id, message):
            seen.append(auth_engine.open)
            return AgentReply("ok", plan_turn([HumanMessage(content=message)]))

    app.dependency_overrides[get_agent] = lambda: Probe()
    try:
        _, headers = login(Role.OWNER)
        assert send(client, headers, uuid.uuid4(), "hi").status_code == 200
    finally:
        app.dependency_overrides.pop(get_agent, None)
    assert seen == [0]


# --- conversations -------------------------------------------------------------------


def test_follow_up_uses_the_same_checkpointed_thread(client, login, agent, executed):
    graph_agent, model = agent([
        tool_call("get_low_stock_items", {"query": "bush"}),
        AIMessage(content="2 bushes are low."),
        AIMessage(content="At 132-2, 1 bush is low."),
    ])
    user, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    assert send(client, headers, conversation, "Which bushes are low on stock?").json()["message"] == "2 bushes are low."
    second = send(client, headers, conversation, "What about 132-2?")
    assert second.status_code == 200 and second.json()["message"] == "At 132-2, 1 bush is low."
    # The API sends only the new message; the checkpointer supplies the earlier turn.
    sent = [m.content for m in model.calls[2] if isinstance(m, HumanMessage)]
    assert sent == ["Which bushes are low on stock?", "What about 132-2?"]
    assert "2 bushes are low." in [m.content for m in model.calls[2] if isinstance(m, AIMessage)]
    assert len(history(graph_agent, user, conversation)) == 6


def test_same_conversation_id_from_two_users_gives_two_threads(client, login, agent):
    graph_agent, model = agent([AIMessage(content="alice's answer"), AIMessage(content="mallory's answer")])
    alice, alice_headers = login(Role.OWNER)
    mallory, mallory_headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    send(client, alice_headers, conversation, "alice's private question")
    response = send(client, mallory_headers, conversation, "show me the history")
    assert response.json()["message"] == "mallory's answer"
    # Mallory's turn saw only Mallory's message, and Alice's thread is untouched.
    assert [m.content for m in model.calls[1] if not isinstance(m, SystemMessage)] == ["show me the history"]
    assert thread_id_for(alice, conversation) != thread_id_for(mallory, conversation)
    assert [m.content for m in history(graph_agent, alice, conversation)] == ["alice's private question", "alice's answer"]
    assert "alice" not in response.text


def test_equivalent_uuid_spellings_reach_the_same_thread(client, login, agent):
    graph_agent, _ = agent([AIMessage(content="one"), AIMessage(content="two")])
    user, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    send(client, headers, conversation, "first")
    response = send(client, headers, str(conversation).upper(), "second")
    assert response.json()["conversation_id"] == str(conversation)
    assert len(history(graph_agent, user, conversation)) == 4


# --- response contract ---------------------------------------------------------------


def test_response_serializes_the_agent_reply(client, login, agent, replies):
    agent([AIMessage(content="Hello!")])
    user, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    response = send(client, headers, conversation, "hi")
    reply = replies[1]
    expected = ChatResponse(
        conversation_id=conversation, message=reply.text, plan=reply.plan, mutation=reply.mutation, data=reply.data
    )
    assert response.json() == expected.model_dump(mode="json")
    assert set(response.json()) == {"conversation_id", "message", "plan", "mutation", "data"}
    assert response.json()["mutation"] is None
    assert response.json()["data"] == {"datasets": [], "metrics": []}
    # Nothing internal: no thread id, user id, prompt or graph state.
    for internal in (thread_id_for(user, conversation), str(user.id), "business owner", "tool_calls", "checkpoint"):
        assert internal not in response.text
    assert_no_sensitive_data(response.text)


def test_knowledge_question_passes_through_retrieval_and_planning(client, login, agent, executed):
    executed["search_knowledge"] = {"results": [{"title": "Chequered Plate"}], "min_similarity": 0.3}
    _, model = agent([tool_call("search_knowledge", {"query": "chequered plate"}),
                      AIMessage(content="A chequered plate is an anti-skid steel plate.")])
    _, headers = login(Role.OWNER)
    body = send(client, headers, uuid.uuid4(), "What is a chequered plate?").json()
    assert body["message"] == "A chequered plate is an anti-skid steel plate."
    assert body["plan"]["intent"] == "knowledge" and body["plan"]["source"] == "semantic_retrieval"
    assert body["plan"]["presentation"] == "text" and body["plan"]["datasets"] == []
    assert "search_knowledge" in model.bound_tools[0]


def test_presentation_plan_is_exposed(client, login, agent, executed):
    executed["get_low_stock_items"] = {"items": [{"item_code": f"BO-{i}", "quantity": i} for i in range(3)]}
    agent([tool_call("get_low_stock_items", {}), AIMessage(content="| item | qty |")])
    _, headers = login(Role.OWNER)
    plan = send(client, headers, uuid.uuid4(), "Show low stock items as a table").json()["plan"]
    assert plan["requested_presentation"] == "table" and plan["presentation"] == "table"
    assert [d["tool"] for d in plan["datasets"]] == ["get_low_stock_items"]


# --- mutations (Layer 8 functions replaced, no database) --------------------------------


def test_proposal_is_exposed_structurally(client, login, agent, layer8):
    _, model = agent([tool_call(MOVE, VALID[MOVE])])
    user, headers = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    body = send(client, headers, conversation, "Add 2 kg of BO-MC-0100 at 132-1").json()
    assert body["mutation"] == {
        "proposal_id": "p1", "operation": MOVE, "state": "confirmation_required",
        "message": "Please confirm: add 2 kg.", "entity_id": None, "details": {},
    }
    assert body["message"] == "Please confirm: add 2 kg."
    assert body["plan"]["intent"] == "operational"
    assert layer8["propose"] == [(user.id, thread_id_for(user, conversation), MOVE)] and layer8["confirm"] == []


def test_confirmation_is_the_next_message_in_the_conversation(client, login, agent, layer8):
    _, model = agent([tool_call(MOVE, VALID[MOVE])])
    user, headers = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    send(client, headers, conversation, "Add 2 kg of BO-MC-0100 at 132-1")
    body = send(client, headers, conversation, "Yes, proceed").json()
    assert body["mutation"]["state"] == "executed" and body["mutation"]["proposal_id"] == "p1"
    assert body["message"] == "Done: added 2 kg."
    assert layer8["confirm"] == [(user.id, thread_id_for(user, conversation), "p1")]
    assert len(model.calls) == 1  # Layer 8 executed it; neither the model nor the API did


def test_cancellation_is_the_next_message_in_the_conversation(client, login, agent, layer8):
    agent([tool_call(MOVE, VALID[MOVE])])
    _, headers = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    send(client, headers, conversation, "Add 2 kg of BO-MC-0100 at 132-1")
    body = send(client, headers, conversation, "cancel").json()
    assert body["mutation"]["state"] == "cancelled" and layer8["confirm"] == []


def test_confirmation_in_another_users_conversation_does_nothing(client, login, agent, layer8):
    _, model = agent([tool_call(MOVE, VALID[MOVE]), AIMessage(content="Nothing is waiting for confirmation.")])
    _, owner_headers = login(Role.INVENTORY_MANAGER)
    _, other_headers = login(Role.INVENTORY_MANAGER)
    conversation = uuid.uuid4()
    send(client, owner_headers, conversation, "Add 2 kg of BO-MC-0100 at 132-1")
    body = send(client, other_headers, conversation, "confirm").json()
    assert body["mutation"] is None and layer8["confirm"] == []


@pytest.mark.parametrize("name", [MOVE, ORDER])
def test_owner_cannot_change_data_even_when_the_model_calls_a_mutation(client, login, agent, layer8, name,
                                                                      monkeypatch):
    # The real executor, with no database: the refusal comes from the server's permission check.
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())
    monkeypatch.setattr(mutations, "get_engine", lambda: NoDatabase())
    _, model = agent([tool_call(name, VALID[name]), AIMessage(content="Owners cannot make changes."),
                      AIMessage(content="There is nothing to confirm.")])
    _, headers = login(Role.OWNER)
    conversation = uuid.uuid4()

    body = send(client, headers, conversation, "Add 2 kg of BO-MC-0100 at 132-1").json()
    assert body["mutation"] is None and body["message"] == "Owners cannot make changes."
    assert name not in model.bound_tools[0]  # never offered, so this call came from the model alone
    [refusal] = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert json.loads(refusal.content) == {"error": f"You do not have permission to use {name}"}

    assert send(client, headers, conversation, "confirm").json()["mutation"] is None
    assert layer8 == {"propose": [], "confirm": [], "cancel": []}


def test_api_never_executes_changes_itself():
    source = open(chat_api.__file__).read()
    for forbidden in ("mutations", "confirm_proposal", "cancel_proposal", "inventory", "procurement",
                      "graph.invoke", "StateGraph", "checkpointer.get", "get_connection"):
        assert forbidden not in source.replace("postgres_checkpointer", "")


# --- errors ------------------------------------------------------------------------------


class Raising:
    def __init__(self, error):
        self.error = error

    def respond(self, user, conversation_id, message):
        raise self.error


@pytest.fixture
def failing(client, login):
    _, headers = login(Role.OWNER)

    def post(error):
        app.dependency_overrides[get_agent] = lambda: Raising(error)
        try:
            return send(client, headers, uuid.uuid4(), "hi")
        finally:
            app.dependency_overrides.pop(get_agent, None)

    return post


LEAKY = "postgresql+psycopg://admin:hunter2@db.internal:5432/prod SELECT * FROM users /srv/app/main.py sk-abc123"


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (AgentInputError("message is required"), 422, "message is required"),
        (AgentUnavailableError("The assistant is temporarily unavailable; please try again"), 503,
         "The assistant is temporarily unavailable; please try again"),
        (AgentConfigError("OPENAI_API_KEY is not set in backend/.env"), 503,
         "The assistant is temporarily unavailable; please try again"),
        (AgentError(ITERATION_LIMIT_REPLY), 500, ITERATION_LIMIT_REPLY),
        (PermissionDenied(Role.OWNER, Permission.PROCUREMENT_WRITE), 403, "Forbidden"),
        (NotFoundError("Item X not found"), 404, "Item X not found"),
        (ValidationError("quantity must be positive"), 422, "quantity must be positive"),
        (InsufficientStockError("Only 2 kg in stock"), 409, "Only 2 kg in stock"),
        (ConflictError("The change conflicts with existing data; nothing was saved"), 409,
         "The change conflicts with existing data; nothing was saved"),
        (OperationFailedError("The operation could not be completed; nothing was saved"), 503,
         "The operation could not be completed; nothing was saved"),
        (BusinessError("Not allowed in this state"), 400, "Not allowed in this state"),
    ],
)
def test_expected_errors_map_to_safe_responses(failing, error, status, detail):
    response = failing(error)
    assert response.status_code == status and response.json() == {"detail": detail}


@pytest.mark.parametrize("error", [RuntimeError(LEAKY), KeyError(LEAKY), ConnectionError(LEAKY)])
def test_unexpected_errors_leak_nothing(failing, error, caplog):
    response = failing(error)
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong while answering; please try again"}
    for fragment in ("hunter2", "postgresql", "SELECT", "/srv", "sk-abc", "Traceback"):
        assert fragment not in response.text
    assert "Chat turn failed" in caplog.text  # logged server-side


def test_model_failure_through_the_real_agent(client, login, agent):
    agent([RuntimeError("401 Incorrect API key provided: sk-abc...")])
    _, headers = login(Role.OWNER)
    response = send(client, headers, uuid.uuid4(), "hi")
    assert response.status_code == 503 and "sk-abc" not in response.text


def test_unconfigured_agent_is_unavailable(client, login, monkeypatch):
    def unavailable():
        raise AgentConfigError("OPENAI_API_KEY is not set in backend/.env")

    monkeypatch.setattr(chat_api.agent_runtime, "get", unavailable)
    _, headers = login(Role.OWNER)
    response = send(client, headers, uuid.uuid4(), "hi")
    assert response.status_code == 503 and ".env" not in response.text and "OPENAI" not in response.text


def test_agent_is_created_once_and_its_pool_closed(monkeypatch):
    events = []

    @contextmanager
    def fake_checkpointer():
        events.append("open")
        try:
            yield InMemorySaver()
        finally:
            events.append("close")

    attempts = iter([AgentConfigError("AGENT_MODEL is not set"), None, None])

    def fake_create(checkpointer):
        error = next(attempts)
        if error:
            raise error
        return Agent(ScriptedChatModel(), checkpointer, 3)

    monkeypatch.setattr(chat_api, "postgres_checkpointer", fake_checkpointer)
    monkeypatch.setattr(chat_api, "create_agent", fake_create)
    runtime = chat_api.AgentRuntime()
    with pytest.raises(AgentConfigError):
        runtime.get()
    assert events == ["open", "close"]  # a failed start leaves no pool open
    first = runtime.get()
    assert runtime.get() is first and events == ["open", "close", "open"]
    runtime.close()
    assert events == ["open", "close", "open", "close"]


# --- same-conversation concurrency -----------------------------------------------------------


class Slow:
    """An agent whose turns take a while and record how many overlap per thread."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.guard = threading.Lock()

    def respond(self, user, conversation_id, message):
        key = thread_id_for(user, conversation_id)
        with self.guard:
            self.active[key] = self.active.get(key, 0) + 1
            self.peak[key] = max(self.peak.get(key, 0), self.active[key])
        time.sleep(self.seconds)
        with self.guard:
            self.active[key] -= 1
        return AgentReply(f"answered {message}", plan_turn([HumanMessage(content=message)]))


@pytest.fixture
def slow(client, login, monkeypatch):
    def install(seconds, wait_seconds):
        locks = ConversationLocks(wait_seconds)
        monkeypatch.setattr(chat_api, "conversation_locks", locks)
        agent = Slow(seconds)
        app.dependency_overrides[get_agent] = lambda: agent
        return agent, locks

    yield install
    app.dependency_overrides.pop(get_agent, None)


def test_same_conversation_turns_are_serialized(client, login, slow):
    agent, locks = slow(seconds=0.3, wait_seconds=5)
    _, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda i: send(client, headers, conversation, f"m{i}"), range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert list(agent.peak.values()) == [1]
    assert len(locks) == 0


def test_busy_conversation_is_refused_after_a_bounded_wait(client, login, slow):
    agent, locks = slow(seconds=1.0, wait_seconds=0.1)
    _, headers = login(Role.OWNER)
    conversation = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, client, headers, conversation, "first")
        time.sleep(0.3)
        second = pool.submit(send, client, headers, conversation, "second").result()
        assert first.result().status_code == 200
    assert second.status_code == 409
    assert second.json() == {
        "detail": "Another message in this conversation is still being answered; please wait for it to finish"
    }
    assert len(locks) == 0


def test_other_conversations_and_users_are_not_blocked(client, login, slow):
    agent, locks = slow(seconds=0.5, wait_seconds=0.1)
    _, alice = login(Role.OWNER)
    _, bob = login(Role.OWNER)
    conversation = uuid.uuid4()
    calls = [(alice, conversation), (alice, uuid.uuid4()), (bob, conversation)]  # same UUID, other user
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda c: send(client, c[0], c[1], "hi"), calls))
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(agent.peak) == 3 and len(locks) == 0


def test_locks_are_released_and_dropped_after_errors():
    locks = ConversationLocks(wait_seconds=0.05)
    with pytest.raises(RuntimeError):
        with locks.hold("t"):
            raise RuntimeError("boom")
    assert len(locks) == 0
    with locks.hold("t"):
        assert len(locks) == 1
        with pytest.raises(ConversationBusy):
            with locks.hold("t"):
                pass
        assert len(locks) == 1  # the refused waiter left no entry of its own
    assert len(locks) == 0
    with locks.hold("t"):  # usable again
        pass


def test_locks_do_not_grow_with_abandoned_conversations():
    locks = ConversationLocks(wait_seconds=1)
    for _ in range(1000):
        with locks.hold(str(uuid.uuid4())):
            pass
    assert len(locks) == 0


# --- live database (demo users, rolled back) ------------------------------------------------


def demo_headers(user):
    with get_engine().connect() as conn:
        version = get_user_by_id(conn, user.id)["token_version"]
    return {"Authorization": f"Bearer {create_access_token(user.id, version)[0]}"}


def test_live_no_connection_is_held_while_the_agent_runs(client, demo_users):
    seen = []

    class Probe:
        def respond(self, user, conversation_id, message):
            seen.append((user, get_engine().pool.checkedout()))
            return AgentReply("ok", plan_turn([HumanMessage(content=message)]))

    user = demo_users["procurement_manager"]
    headers = demo_headers(user)
    app.dependency_overrides[get_agent] = lambda: Probe()
    try:
        assert send(client, headers, uuid.uuid4(), "hi").status_code == 200
    finally:
        app.dependency_overrides.pop(get_agent, None)
    assert seen == [(user, 0)]  # real token, real lookup, connection already back in the pool


def test_live_stock_movement_proposed_and_confirmed_through_the_api(client, agent, db, inv_user, stocked, monkeypatch):
    monkeypatch.setattr(agent_tools, "get_engine", lambda: NoDatabase())  # no read tool is needed
    headers = demo_headers(inv_user)
    quantity = stock(db, stocked)
    _, model = agent([tool_call(MOVE, move(stocked, quantity=1))])
    conversation = uuid.uuid4()

    proposed = send(client, headers, conversation, f"Add 1 of {stocked['item_code']} at {stocked['location']}").json()
    assert proposed["mutation"]["state"] == "confirmation_required" and proposed["mutation"]["proposal_id"]
    assert stock(db, stocked) == quantity  # nothing changed yet

    done = send(client, headers, conversation, "yes").json()
    assert done["mutation"]["state"] == "executed"
    assert done["mutation"]["proposal_id"] == proposed["mutation"]["proposal_id"]
    assert done["mutation"]["entity_id"] and stock(db, stocked) == quantity + 1

    again = send(client, headers, conversation, "yes").json()
    assert again["mutation"]["state"] == MutationState.ALREADY_EXECUTED and stock(db, stocked) == quantity + 1
    assert len(model.calls) == 1
