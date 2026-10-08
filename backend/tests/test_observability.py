"""Layer 16: LangSmith tracing is optional, privacy-first and passive.

No test talks to LangSmith. Enabled tests use a real langsmith Client whose transport is
replaced, so they check the exact payloads the SDK would upload, after its redaction.
"""

import json
import os
import time
import uuid

import httpx
import pytest
from langchain_core.tracers import langchain as lc_tracer
from langchain_openai import ChatOpenAI
from langchain_core.tracers.context import collect_runs
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import Client
from langsmith.utils import LangSmithNotFoundError
from pydantic import SecretStr

from app import observability
from app.agent import tools as agent_tools
from app.agent.graph import Agent, thread_id_for
from app.auth.permissions import Role
from app.config import Settings, get_settings
from app.rag import search as rag_search
from app.rag.embeddings import EmbeddingError
from tests.fakes import FakeEmbedder, make_agent_user
from tests.test_agent_tools import FakeEngine, replace_operation

SECRETS = ("SECRET-QUESTION", "SECRET-ARG", "SECRET-ROW", "SECRET-ANSWER", "SECRET-DOC")
TEST_KEY = "lsv2_test_" + uuid.uuid4().hex  # never a real key; checked only as a boolean


def settings(**overrides) -> Settings:
    """Settings from explicit values only (no .env file), with the given LangSmith overrides."""
    base = {"database_url": "postgresql://u:p@localhost:5432/db", "jwt_secret_key": "x" * 32}
    return Settings(_env_file=None, **base, **overrides)


@pytest.fixture
def configure(monkeypatch):
    """configure(**settings) -> the client get_langsmith_client() returns for them."""

    def apply(**overrides):
        monkeypatch.setattr(observability, "get_settings", lambda: settings(**overrides))
        observability.get_langsmith_client.cache_clear()
        return observability.get_langsmith_client()

    yield apply
    observability.get_langsmith_client.cache_clear()


@pytest.fixture
def captured(configure, monkeypatch):
    """Tracing enabled with a client whose uploads are captured instead of sent."""
    client = configure(langsmith_tracing=True, langsmith_api_key=TEST_KEY, langsmith_project="layer16-tests")
    posts, patches = [], []
    assert client is not None
    monkeypatch.setattr(Client, "_create_run", lambda self, run, **kwargs: posts.append(run))
    monkeypatch.setattr(Client, "_update_run", lambda self, run, **kwargs: patches.append(run))

    def runs():
        wait_for_all_tracers()  # LangChain's tracer uploads from a thread pool
        merged: dict[str, dict] = {}
        for payload in [*posts, *patches]:
            merged.setdefault(str(payload["id"]), {}).update({k: v for k, v in payload.items() if v is not None})
        return list(merged.values()), json.dumps([*posts, *patches], default=str)

    return runs


@pytest.fixture
def read_tool(monkeypatch):
    """get_low_stock_items through the real executor, answered by a stub operation (no database)."""
    monkeypatch.setattr(agent_tools, "get_engine", lambda: FakeEngine())
    replace_operation(monkeypatch, "get_low_stock_items",
                      lambda conn, user, **kwargs: {"items": [{"item_code": "SECRET-ROW", "quantity": "0"}] * 2})


def completion(message: dict) -> dict:
    """An OpenAI chat completion, as the API returns it."""
    return {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-5.4-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}


def openai_model() -> ChatOpenAI:
    """The production model class on a scripted HTTP transport: real serialization, no network."""
    replies = [
        completion({"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "get_low_stock_items", "arguments": json.dumps({"query": "SECRET-ARG"})}}]}),
        completion({"content": "SECRET-ANSWER"}),
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=replies.pop(0)))
    return ChatOpenAI(model="gpt-5.4-mini", api_key=TEST_KEY, max_retries=0,
                      http_client=httpx.Client(transport=transport))


def turn(user):
    conversation = uuid.uuid4()
    reply = Agent(openai_model(), InMemorySaver(), 12).respond(user, conversation, "Show me low-stock items SECRET-QUESTION")
    return reply, conversation


def by_name(runs, name):
    return [run for run in runs if run.get("name") == name]


def metadata(run):
    return (run.get("extra") or {}).get("metadata") or {}


# --- configuration --------------------------------------------------------------------------------


def test_tracing_is_off_by_default_with_a_default_project():
    defaults = settings()
    assert defaults.langsmith_tracing is False and defaults.langsmith_api_key is None
    assert defaults.langsmith_project == "lecxe-chatbot"
    assert settings(langsmith_project="  ").langsmith_project == "lecxe-chatbot"
    assert settings(langsmith_project="demo").langsmith_project == "demo"
    assert settings(langsmith_api_key="  ").langsmith_api_key is None


def test_api_key_is_a_secret():
    configured = settings(langsmith_tracing=True, langsmith_api_key=TEST_KEY)
    assert isinstance(configured.langsmith_api_key, SecretStr)
    leaked = TEST_KEY in repr(configured) or TEST_KEY in str(configured.model_dump())
    assert not leaked


@pytest.mark.parametrize("value, enabled", [("true", True), ("1", True), ("false", False), ("0", False)])
def test_tracing_flag(value, enabled):
    assert settings(langsmith_tracing=value).langsmith_tracing is enabled


def test_enabled_without_a_key_stays_off(configure, caplog):
    assert configure(langsmith_tracing=True) is None
    assert "LANGSMITH_API_KEY is not set" in caplog.text


def test_enabled_client_redacts_everything_by_construction(configure):
    client = configure(langsmith_tracing=True, langsmith_api_key=TEST_KEY)
    assert client._hide_inputs is True and client._hide_outputs is True
    assert client._hide_metadata is observability.safe_metadata and client._omit_traced_runtime_info
    assert TEST_KEY not in repr(client)


def test_the_real_settings_default_to_no_tracing_in_tests():
    # The suite must never trace by accident: backend/.env has no LANGSMITH_TRACING.
    assert get_settings().langsmith_tracing is False


# --- disabled ---------------------------------------------------------------------------------------


def test_disabled_tracing_creates_no_client_and_overrides_the_sdk_environment(configure, read_tool, monkeypatch):
    # Even with the SDK's own variables set, nothing is created or sent when Settings say no.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_KEY)
    created = []
    monkeypatch.setattr(observability, "Client", lambda *a, **k: created.append("client"))
    monkeypatch.setattr(lc_tracer.LangChainTracer, "__init__", lambda *a, **k: created.append("tracer"))
    assert configure(langsmith_tracing=False, langsmith_api_key=TEST_KEY) is None

    reply, _ = turn(make_agent_user(Role.INVENTORY_MANAGER))
    assert reply.text == "SECRET-ANSWER" and reply.data.datasets
    assert created == []


def test_disabled_spans_are_plain_dicts(configure):
    configure(langsmith_tracing=False)
    with observability.traced_step("x", "tool", tool="t") as span:
        span["outcome"] = "success"
    assert span == {"tool": "t", "outcome": "success"}


# --- enabled: structure and redaction -------------------------------------------------------------------


def test_turn_trace_has_structure_but_no_content(captured, read_tool):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    reply, conversation = turn(user)
    runs, raw = captured()

    # The application behaves exactly as without tracing.
    assert reply.text == "SECRET-ANSWER" and [r["item_code"] for r in reply.data.datasets[0].rows] == ["SECRET-ROW"] * 2

    names = {run["name"] for run in runs}
    assert {"LangGraph", "confirmation", "agent", "tools", "get_low_stock_items"} <= names
    assert all(run.get("session_name") in (None, "layer16-tests") for run in runs)

    # Nothing the user, the model, the prompt or the database said leaves the process.
    for secret in (*SECRETS, str(user.id), user.email, thread_id_for(user, conversation), str(conversation),
                   "inventory manager", TEST_KEY):
        assert secret not in raw, secret
    assert all(not run.get("inputs") and not run.get("outputs") for run in runs)

    # Model runs keep their token usage; every run is tagged with the role only.
    model_runs = [run for run in runs if run.get("run_type") == "llm"]
    assert len(model_runs) == 2 and all(metadata(run)["usage_metadata"]["total_tokens"] == 150 for run in model_runs)
    assert all(metadata(run).get("role") == "inventory_manager" for run in runs)
    assert all("thread_id" not in metadata(run) for run in runs)


def test_tool_span_is_a_child_of_the_tools_node_with_safe_facts_only(captured, read_tool):
    turn(make_agent_user(Role.INVENTORY_MANAGER))
    runs, _ = captured()
    [span] = by_name(runs, "get_low_stock_items")
    [node] = by_name(runs, "tools")
    assert span["run_type"] == "tool" and span["parent_run_id"] == node["id"]
    assert {k: metadata(span).get(k) for k in ("tool", "mutation", "outcome")} == {
        "tool": "get_low_stock_items", "mutation": False, "outcome": "success"}
    assert "error_kind" not in metadata(span) and not span.get("error")


def test_failed_tool_records_its_kind_not_its_arguments(captured):
    user = make_agent_user(Role.INVENTORY_MANAGER)
    with observability.traced_turn(role=user.role.value):
        agent_tools.execute_tool(user, "get_low_stock_items", {"query": "SECRET-ARG", "user_id": "x"})
        agent_tools.execute_tool(user, "SECRET-ARG made-up tool", {})
    runs, raw = captured()
    assert "SECRET-ARG" not in raw
    [invalid] = by_name(runs, "get_low_stock_items")
    assert metadata(invalid)["outcome"] == "error" and metadata(invalid)["error_kind"] == "invalid_arguments"
    [unknown] = by_name(runs, "unknown_tool")
    assert metadata(unknown)["error_kind"] == "unknown_tool" and "tool" not in metadata(unknown)


def test_mutation_proposal_span_shows_only_that_a_change_was_proposed(captured, monkeypatch):
    from app.agent import mutations
    from tests.test_mutations import MOVE, VALID

    status = mutations.MutationStatus(proposal_id="p1", operation=MOVE, state=mutations.MutationState.CONFIRMATION_REQUIRED,
                                      message="Please confirm: SECRET-ROW", details={"item_code": "SECRET-ROW"})
    monkeypatch.setattr(mutations, "propose", lambda *args: status)
    user = make_agent_user(Role.INVENTORY_MANAGER)
    with observability.traced_turn(role=user.role.value):
        result = agent_tools.execute_tool(user, MOVE, {**VALID[MOVE], "notes": "SECRET-ARG"}, thread_id="t")
    assert result.mutation is status  # unchanged: still only a proposal
    runs, raw = captured()
    [span] = by_name(runs, MOVE)
    assert metadata(span)["outcome"] == "proposed" and metadata(span)["mutation"] is True
    assert "SECRET-ARG" not in raw and "SECRET-ROW" not in raw and "p1" not in json.dumps(metadata(span))


# --- enabled: retrieval ------------------------------------------------------------------------------------


@pytest.fixture
def retrieval(monkeypatch):
    rows = [{"source": "item", "source_id": f"i{n}", "title": "SECRET-DOC", "similarity": s,
             "content": "SECRET-DOC", "metadata": {}} for n, s in enumerate((0.8123, 0.4567))]
    monkeypatch.setattr(rag_search.repo, "search", lambda conn, vector, **kwargs: rows)

    def use(embedder):
        monkeypatch.setattr(rag_search, "get_embedder", lambda: embedder)

    return use


def test_retrieval_span_has_counts_and_similarity_only(captured, retrieval):
    retrieval(FakeEmbedder(default=(0.123456789, 0.0, 0.0)))
    user = make_agent_user(Role.INVENTORY_MANAGER)
    with observability.traced_turn(role=user.role.value):
        result = rag_search.search_knowledge(None, user, "SECRET-QUESTION about plates", top_k=3, source="item")
    assert [r["content"] for r in result["results"]] == ["SECRET-DOC", "SECRET-DOC"]  # unchanged for the agent
    runs, raw = captured()
    [span] = by_name(runs, "semantic_retrieval")
    assert span["run_type"] == "retriever"
    facts = {k: metadata(span).get(k) for k in ("source", "top_k", "results", "similarity_max", "similarity_min")}
    assert facts == {"source": "item", "top_k": 3, "results": 2, "similarity_max": 0.812, "similarity_min": 0.457}
    for leak in ("SECRET-QUESTION", "SECRET-DOC", "0.123456789", "i0"):
        assert leak not in raw, leak


def test_failed_span_records_only_the_exception_type(captured, retrieval):
    retrieval(FakeEmbedder(error=EmbeddingError("SECRET-DOC upstream detail")))
    user = make_agent_user(Role.INVENTORY_MANAGER)
    with observability.traced_turn(role=user.role.value), pytest.raises(EmbeddingError, match="SECRET-DOC"):
        rag_search.search_knowledge(None, user, "SECRET-QUESTION")  # the application still sees the real error
    runs, raw = captured()
    [span] = by_name(runs, "semantic_retrieval")
    assert span["error"] == "EmbeddingError" and metadata(span)["error_type"] == "EmbeddingError"
    assert "SECRET" not in raw and "Traceback" not in raw


# --- enabled: availability ---------------------------------------------------------------------------------


def test_failing_uploads_never_fail_a_turn(configure, read_tool, monkeypatch):
    # Stricter than an unreachable server (whose uploads fail in the SDK's background thread):
    # every upload raises in the calling thread.
    def unreachable(self, run, **kwargs):
        raise ConnectionError("LangSmith unreachable")

    monkeypatch.setattr(Client, "_create_run", unreachable)
    monkeypatch.setattr(Client, "_update_run", unreachable)
    configure(langsmith_tracing=True, langsmith_api_key=TEST_KEY)
    reply, _ = turn(make_agent_user(Role.INVENTORY_MANAGER))
    wait_for_all_tracers()
    assert reply.text == "SECRET-ANSWER" and reply.data.datasets


# --- opt-in live check ----------------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(os.getenv("LANGSMITH_LIVE") != "1", reason="set LANGSMITH_LIVE=1 (and LANGSMITH_* in backend/.env)")
def test_live_trace_reaches_langsmith_redacted(read_tool):
    """One small trace to the configured project; no database, no OpenAI call (scripted transport)."""
    observability.get_langsmith_client.cache_clear()
    client = observability.get_langsmith_client()
    assert client is not None, "LANGSMITH_TRACING=true and LANGSMITH_API_KEY are needed in backend/.env"
    with collect_runs() as collected:
        reply, _ = turn(make_agent_user(Role.INVENTORY_MANAGER))
    assert reply.text == "SECRET-ANSWER"
    wait_for_all_tracers()
    client.flush()
    root_id = collected.traced_runs[0].id
    for _ in range(20):  # ingestion is asynchronous
        try:
            stored = client.read_run(root_id)
            break
        except LangSmithNotFoundError:
            time.sleep(1)
    else:
        pytest.fail("the trace did not appear in LangSmith")
    children = list(client.list_runs(trace_id=stored.trace_id))
    assert stored.name == "LangGraph" and not stored.inputs and not stored.outputs
    assert any(run.name == "get_low_stock_items" and run.run_type == "tool" for run in children)
    assert all(not run.inputs and not run.outputs for run in children)
