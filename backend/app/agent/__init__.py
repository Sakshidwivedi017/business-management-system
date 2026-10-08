"""Conversational agent core (Layer 5): read-only tools over Layer 4 operations.

Typical use (Layer 9 will do this at startup):

    with postgres_checkpointer() as checkpointer:
        agent = create_agent(checkpointer)
        reply = agent.chat(user, conversation_id, "Which items are low on stock?")

`agent.respond(...)` returns the same text with its Layer 7 ResponsePlan
(intent, source and presentation), derived deterministically from the conversation,
and the Layer 8 MutationStatus when the turn proposed, executed or cancelled a change.
confirm_proposal / cancel_proposal act on a pending change for an authenticated user
and thread (thread_id_for), e.g. from a confirmation button.
"""

from app.agent.checkpoint import postgres_checkpointer
from app.agent.errors import AgentConfigError, AgentError, AgentInputError, AgentUnavailableError
from app.agent.graph import Agent, AgentReply, create_agent, thread_id_for
from app.agent.mutations import MutationState, MutationStatus, cancel_proposal, confirm_proposal
from app.agent.planning import ResponsePlan, plan_turn

__all__ = [
    "Agent",
    "AgentConfigError",
    "AgentError",
    "AgentInputError",
    "AgentReply",
    "AgentUnavailableError",
    "MutationState",
    "MutationStatus",
    "ResponsePlan",
    "cancel_proposal",
    "confirm_proposal",
    "create_agent",
    "plan_turn",
    "postgres_checkpointer",
    "thread_id_for",
]
