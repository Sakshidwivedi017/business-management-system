"""search_knowledge as an agent tool, against the real index with a fake query embedder (no API credits).

The fake returns the stored embedding of the 'Chequered Plate' classification
entry, so that entry is the top hit with similarity 1.0.
"""

import json
import logging
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text

from app.agent.graph import Agent
from app.agent.tools import TOOLS, available_tools, execute_tool
from app.auth.permissions import ROLE_PERMISSIONS, Permission, Role
from app.db.connection import get_engine
from app.db.repositories import knowledge as repo
from app.rag import search as rag_search
from app.rag.embeddings import EmbeddingError
from tests.fakes import FakeEmbedder, NoDatabase, ScriptedChatModel, make_agent_user, tool_call

CHEQUERED = "Chequered Plate"


@pytest.fixture
def plate_query(monkeypatch, live_db):
    with get_engine().connect() as conn:
        if not repo.index_exists(conn):
            pytest.skip("semantic index not created (python -m app.rag.index --create-schema)")
        row = conn.execute(
            text("SELECT embedding::text AS v, embedding_model FROM rag.documents "
                 "WHERE source = 'item_classification' AND metadata->>'name' = :n"),
            {"n": CHEQUERED},
        ).mappings().one()
    fake = FakeEmbedder(default=json.loads(row["v"]), model=row["embedding_model"])
    monkeypatch.setattr(rag_search, "get_embedder", lambda: fake)
    return fake


def run(user, responses):
    model = ScriptedChatModel(responses=responses)
    reply = Agent(model, InMemorySaver(), 12).chat(user, uuid.uuid4(), "question")
    return reply, model


def tool_results(model):
    return [m for m in model.calls[-1] if isinstance(m, ToolMessage)]


# --- tool ----------------------------------------------------------------------


def test_registered_read_only_for_every_role():
    tool = TOOLS["search_knowledge"]
    assert tool.mutation is False and tool.permission is Permission.INVENTORY_READ
    for role in Role:
        assert "search_knowledge" in {t.name for t in available_tools(make_agent_user(role))}
    assert set(tool.openai_schema()["function"]["parameters"]["properties"]) == {"query", "top_k", "source"}


@pytest.mark.parametrize(
    "args",
    [
        {"query": "x", "embedding_model": "text-embedding-3-large"},
        {"query": "x", "min_similarity": 0},
        {"query": "x", "threshold": 0},
        {"query": "x", "table": "users"},
        {"query": "x", "embedding": [0.1, 0.2]},
        {"query": "x", "sql": "SELECT * FROM users"},
        {"query": "x", "user_id": str(uuid.uuid4()), "role": "owner"},
        {"query": "x", "top_k": 11},
        {"query": "x", "top_k": 1000000},
        {"query": "x" * 501},
        {"query": "x", "source": "users"},
        {},
    ],
)
def test_arguments_are_strict(args, monkeypatch):
    monkeypatch.setattr("app.agent.tools.get_engine", lambda: NoDatabase())
    result = execute_tool(make_agent_user(Role.OWNER), "search_knowledge", args)
    assert result.is_error and json.loads(result.content)["error"].startswith("Invalid arguments for search_knowledge")


def test_role_without_inventory_read_is_denied(monkeypatch):
    monkeypatch.setitem(ROLE_PERMISSIONS, Role.INVENTORY_MANAGER, frozenset())
    monkeypatch.setattr("app.agent.tools.get_engine", lambda: NoDatabase())
    user = make_agent_user(Role.INVENTORY_MANAGER)
    assert "search_knowledge" not in {t.name for t in available_tools(user)}
    assert json.loads(execute_tool(user, "search_knowledge", {"query": "plate"}).content) == {
        "error": "You do not have permission to use search_knowledge"
    }


def test_executor_returns_ranked_sourced_results(plate_query):
    result = execute_tool(make_agent_user(Role.INVENTORY_MANAGER), "search_knowledge", {"query": "anti-skid plate"})
    assert not result.is_error
    hits = json.loads(result.content)["result"]["results"]
    assert 1 <= len(hits) <= 5
    top = hits[0]
    assert top["source"] == "item_classification" and top["metadata"]["name"] == CHEQUERED
    assert top["similarity"] == 1.0 and top["source_id"] and top["title"].startswith(CHEQUERED)
    assert "IS 3502" in top["content"]  # the real classification-guide text
    assert all(h["similarity"] >= 0.3 for h in hits)
    assert [h["similarity"] for h in hits] == sorted((h["similarity"] for h in hits), reverse=True)


def test_embedding_failure_is_safe(monkeypatch, caplog, live_db):
    secret = "sk-live-should-never-appear-0123456789"
    failing = FakeEmbedder(error=EmbeddingError("Semantic search is temporarily unavailable; please try again later"))
    monkeypatch.setattr(rag_search, "get_embedder", lambda: failing)
    with caplog.at_level(logging.DEBUG):
        result = execute_tool(make_agent_user(Role.OWNER), "search_knowledge", {"query": secret})
    assert json.loads(result.content) == {"error": "Semantic search is temporarily unavailable; please try again later"}
    assert secret not in result.content and secret not in caplog.text


# --- agent -------------------------------------------------------------------------


def test_agent_answers_from_retrieved_context(plate_query):
    reply, model = run(
        make_agent_user(Role.INVENTORY_MANAGER),
        [
            tool_call("search_knowledge", {"query": "anti-skid floor plate", "source": "item_classification"}),
            AIMessage(content="That is a Chequered Plate (IS 3502), per the classification guide."),
        ],
    )
    assert reply.startswith("That is a Chequered Plate")
    [result] = tool_results(model)
    assert result.status == "success"
    hits = json.loads(result.content)["result"]["results"]
    assert hits and all(h["source"] == "item_classification" for h in hits)
    assert hits[0]["metadata"]["name"] == CHEQUERED


def test_agent_combines_semantic_and_structured_tools(plate_query):
    # Semantic search finds the item; the structured tool supplies its live stock.
    first = execute_tool(make_agent_user(Role.OWNER), "search_knowledge", {"query": "anti-skid", "source": "item"})
    item_code = json.loads(first.content)["result"]["results"][0]["metadata"]["item_code"]
    reply, model = run(
        make_agent_user(Role.OWNER),
        [
            tool_call("search_knowledge", {"query": "anti-skid plate", "source": "item"}),
            tool_call("get_item_details", {"item": item_code}),
            AIMessage(content=f"{item_code} is a chequered plate; stock is listed per location."),
        ],
    )
    semantic, structured = tool_results(model)
    assert semantic.name == "search_knowledge" and semantic.status == "success"
    assert structured.name == "get_item_details" and structured.status == "success"
    details = json.loads(structured.content)["result"]
    assert details["item"]["item_code"] == item_code and "stock" in details
    # The semantic result carries no stock figures; those came only from the structured tool.
    assert "quantity" not in semantic.content.lower()
