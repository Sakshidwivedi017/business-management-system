"""Opt-in live agent tests. Skipped unless explicitly enabled:

    AGENT_LIVE_CHECKPOINT=1  writes one temporary thread to the real checkpoint tables, then deletes only it
    AGENT_LIVE_OPENAI=1      makes a few real OpenAI calls (needs OPENAI_API_KEY, AGENT_MODEL and, for
                             semantic search, EMBEDDING_MODEL plus a built index: python -m app.rag.index)
"""

import json
import os
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpoint import postgres_checkpointer
from app.agent.graph import Agent, create_agent, thread_id_for
from app.db.connection import get_engine
from app.rag.search import search_knowledge
from tests.fakes import ScriptedChatModel, tool_call

pytestmark = pytest.mark.live

live_checkpoint = pytest.mark.skipif(os.getenv("AGENT_LIVE_CHECKPOINT") != "1", reason="set AGENT_LIVE_CHECKPOINT=1")
live_openai = pytest.mark.skipif(os.getenv("AGENT_LIVE_OPENAI") != "1", reason="set AGENT_LIVE_OPENAI=1")


@live_checkpoint
def test_conversation_persists_in_postgres(demo_users):
    user = demo_users["inventory_manager"]
    conversation = uuid.uuid4()
    thread_id = thread_id_for(user, conversation)
    model = ScriptedChatModel(
        responses=[
            tool_call("list_locations", {}),
            AIMessage(content="Listed the locations."),
            AIMessage(content="You asked about locations."),
        ]
    )
    with postgres_checkpointer(max_size=2) as saver:
        try:
            assert saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None
            Agent(model, saver, 12).chat(user, conversation, "Which locations exist?")
            # A fresh agent on the same storage continues the same conversation.
            Agent(model, saver, 12).chat(user, conversation, "What did I ask?")
            history = [type(m).__name__ for m in model.calls[-1][1:]]
            assert history == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage", "HumanMessage"]
        finally:
            # Removes only this test's uniquely named thread.
            saver.delete_thread(thread_id)
        assert saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None


@live_openai
def test_real_model_uses_tools_and_follows_up(demo_users):
    agent = create_agent(InMemorySaver())
    user = demo_users["owner"]
    conversation = uuid.uuid4()
    config = {"configurable": {"thread_id": thread_id_for(user, conversation)}}

    reply = agent.chat(user, conversation, "Which stock locations do we have? Just list their names.")
    assert reply and "C-84" in reply
    first = agent.graph.get_state(config).values["messages"]
    assert any(isinstance(m, ToolMessage) and m.name == "list_locations" for m in first)

    reply = agent.chat(user, conversation, "Which of those is not active?")
    assert reply and "Unassigned" in reply

    tool_results = [m for m in agent.graph.get_state(config).values["messages"] if isinstance(m, ToolMessage)]
    assert all("error" not in json.loads(m.content) for m in tool_results)


@live_openai
def test_real_model_respects_role(demo_users):
    agent = create_agent(InMemorySaver())
    user = demo_users["inventory_manager"]
    conversation = uuid.uuid4()
    reply = agent.chat(user, conversation, "Show me our top vendors by spend.")
    assert reply
    config = {"configurable": {"thread_id": thread_id_for(user, conversation)}}
    succeeded = {
        m.name
        for m in agent.graph.get_state(config).values["messages"]
        if isinstance(m, ToolMessage) and m.status == "success"
    }
    assert not {"list_vendors", "get_vendor", "procurement_summary"} & succeeded


@live_openai
def test_real_embedding_search(demo_users):
    # Two tiny query embeddings against the existing index; the corpus is not re-embedded.
    user = demo_users["inventory_manager"]
    with get_engine().connect() as conn:
        found = search_knowledge(conn, user, "anti-skid floor plate", top_k=3)
        unrelated = search_knowledge(conn, user, "who won the football world cup")
    assert any("Chequered Plate" in r["title"] for r in found["results"])
    assert all(r["similarity"] >= found["min_similarity"] for r in found["results"])
    assert unrelated["results"] == [] and "message" in unrelated
