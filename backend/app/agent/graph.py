"""The conversational agent: a LangGraph loop (agent ⇄ tools) with checkpointed messages.

Only messages are persisted. The authenticated user travels in the run context,
so role and permissions are always the ones Layer 3 loaded for this request.
A confirmation gate (Layer 8) runs first in every turn: when the user's message
explicitly confirms or cancels the change proposed just before, it acts on it
and ends the turn without a model call.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from app.agent.datasets import ChatData, chat_data
from app.agent.errors import AgentError, AgentInputError, AgentUnavailableError
from app.agent.llm import create_chat_model
from app.agent.planning import Presentation, ResponsePlan, plan_turn
from app.agent.prompts import response_guidance, system_prompt
from app.agent import mutations
from app.agent.tools import MUTATION_TOOLS, ToolResult, available_tools, execute_tool
from app.auth.permissions import AuthenticatedUser
from app.config import get_settings
from app.observability import traced_turn

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000
ITERATION_LIMIT_REPLY = (
    "I couldn't finish this request within the allowed number of steps. "
    "Please try a narrower question, for example one item or one location."
)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@dataclass(frozen=True)
class AgentContext:
    """Per-run context. Never checkpointed."""

    user: AuthenticatedUser
    thread_id: str | None = None  # derived from the user (thread_id_for); scopes Layer 8 proposals


def build_graph(llm: BaseChatModel, checkpointer: BaseCheckpointSaver | None, max_iterations: int):
    def confirmation(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        # Layer 8: only the user's own explicit reply to a pending proposal executes or cancels it.
        status = mutations.answer_pending(runtime.context.user, runtime.context.thread_id, state["messages"])
        return {"messages": [mutations.status_message(status)]} if status else {}

    def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        user = runtime.context.user
        if _model_calls_this_turn(state["messages"]) >= max_iterations:
            logger.warning("Agent iteration limit (%d) reached", max_iterations)
            return {"messages": [AIMessage(content=ITERATION_LIMIT_REPLY)]}
        tools = available_tools(user)
        model = llm.bind_tools([tool.openai_schema() for tool in tools]) if tools else llm
        messages = [SystemMessage(content=system_prompt(user)), *state["messages"]]
        if isinstance(state["messages"][-1], HumanMessage):
            # Layer 7 guidance at the start of a turn. It goes after the history, so the
            # cacheable prompt prefix is unchanged, and it is never stored in the state.
            guidance = response_guidance(plan_turn(state["messages"]), any(tool.mutation for tool in tools))
            if guidance:
                messages.append(SystemMessage(content=guidance))
        try:
            response = model.invoke(messages)
        except Exception as exc:
            # Provider errors can carry request details; only the type is logged.
            logger.error("Model call failed: %s", type(exc).__name__)
            raise AgentUnavailableError("The assistant is temporarily unavailable; please try again") from None
        return {"messages": [response]}

    def tools(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
        request = state["messages"][-1]
        results = []
        proposal = None
        for call in request.tool_calls:
            logger.info("Agent selected tool %s", call["name"])
            if call["name"] not in MUTATION_TOOLS:
                outcome = execute_tool(runtime.context.user, call["name"], call["args"])
            elif proposal is None:
                outcome = execute_tool(runtime.context.user, call["name"], call["args"],
                                       thread_id=runtime.context.thread_id)
                proposal = outcome.mutation
            else:
                outcome = ToolResult(json.dumps({"error": "Only one change can be proposed at a time; "
                                                          "this one was not proposed"}), is_error=True)
            results.append(
                ToolMessage(
                    content=outcome.content,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error" if outcome.is_error else "success",
                )
            )
        for call in request.invalid_tool_calls:
            # Malformed arguments from the model: answer the call so the conversation stays valid.
            logger.info("Agent produced an unparseable tool call")
            results.append(
                ToolMessage(
                    content=json.dumps({"error": "Tool arguments were not valid JSON"}),
                    tool_call_id=call.get("id") or "invalid",
                    name=call.get("name") or "unknown",
                    status="error",
                )
            )
        if proposal is not None:
            # The turn ends with Layer 8's confirmation request, built from the Layer 4 preview;
            # the model gets no chance to word it, or to claim the change was made.
            results.append(mutations.status_message(proposal))
        return {"messages": results}

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("confirmation", confirmation)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_edge(START, "confirmation")
    graph.add_conditional_edges("confirmation", _continue_unless_answered, ["agent", END])
    graph.add_conditional_edges("agent", _route, ["tools", END])
    graph.add_conditional_edges("tools", _continue_unless_answered, ["agent", END])
    return graph.compile(checkpointer=checkpointer)


def _continue_unless_answered(state: AgentState) -> Literal["agent", "__end__"]:
    """End the turn when Layer 8 has replied (a confirmation request or a mutation outcome)."""
    return END if mutations.is_layer8_message(state["messages"][-1]) else "agent"


def _route(state: AgentState) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls):
        return "tools"
    return END


def _model_calls_this_turn(messages: list[AnyMessage]) -> int:
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage):
            count += 1
    return count


def thread_id_for(user: AuthenticatedUser, conversation_id: uuid.UUID | str) -> str:
    """Checkpoint thread for one of this user's conversations. The user part always comes from auth."""
    try:
        conversation = uuid.UUID(str(conversation_id))
    except ValueError:
        raise AgentInputError("conversation_id must be a UUID") from None
    return f"{user.id}:{conversation}"


@dataclass(frozen=True)
class AgentReply:
    text: str
    plan: ResponsePlan  # Layer 7: what was asked and how the answer is best presented
    # Layer 8: the change this turn proposed, executed, cancelled or refused, if any
    mutation: mutations.MutationStatus | None = None
    # Layer 14: the planned datasets' rows, copied from this turn's own tool results
    data: ChatData = ChatData()


class Agent:
    """Application entry point: `agent.chat(user, conversation_id, message)` returns the reply text;
    `agent.respond(...)` returns it with its response plan and any mutation step."""

    def __init__(self, llm: BaseChatModel, checkpointer: BaseCheckpointSaver, max_iterations: int):
        self.max_iterations = max_iterations
        self.graph = build_graph(llm, checkpointer, max_iterations)

    def chat(self, user: AuthenticatedUser, conversation_id: uuid.UUID | str, message: str) -> str:
        return self.respond(user, conversation_id, message).text

    def respond(self, user: AuthenticatedUser, conversation_id: uuid.UUID | str, message: str) -> AgentReply:
        text = message.strip() if isinstance(message, str) else ""
        if not text:
            raise AgentInputError("message is required")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise AgentInputError(f"message must be at most {MAX_MESSAGE_LENGTH} characters")
        thread_id = thread_id_for(user, conversation_id)
        config = {
            "configurable": {"thread_id": thread_id},
            # Backstop only: each iteration is two steps, and the agent node stops itself first.
            "recursion_limit": 2 * self.max_iterations + 3,
        }
        logger.info("Agent invoked by user %s (%s)", user.id, user.role)
        try:
            # Layer 16: LangGraph and the model report their own runs; traced_turn only scopes them.
            with traced_turn(role=user.role.value):
                state = self.graph.invoke(
                    {"messages": [HumanMessage(content=text)]}, config, context=AgentContext(user, thread_id)
                )
        except GraphRecursionError:
            logger.error("Agent recursion backstop hit")
            raise AgentError(ITERATION_LIMIT_REPLY) from None
        text = state["messages"][-1].text
        # Planned from the messages the run already returned: no extra checkpoint read, nothing stored.
        plan = plan_turn(state["messages"])
        if text == ITERATION_LIMIT_REPLY:
            # An unfinished answer is not presented as a table or chart of partial data.
            plan = plan.model_copy(update={"presentation": Presentation.TEXT})
        mutation = mutations.turn_mutation(state["messages"])
        # A change step is shown as its own card, never as a table or chart.
        data = ChatData() if mutation else chat_data(state["messages"], plan)
        return AgentReply(text, plan, mutation, data)


def create_agent(checkpointer: BaseCheckpointSaver) -> Agent:
    """Agent wired to the configured OpenAI model, e.g. with `postgres_checkpointer()`."""
    return Agent(create_chat_model(), checkpointer, get_settings().agent_max_iterations)
