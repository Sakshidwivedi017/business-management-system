"""POST /api/chat: one user message in, one agent turn out (Layer 9).

The endpoint only orchestrates. Identity comes from Layer 3, the turn runs through
Agent.respond (Layers 5-8: tools, retrieval, response planning, mutation confirmation),
and conversation history lives in the LangGraph checkpointer. A confirmation of a
proposed change is just the next message in the same conversation.

No database connection or transaction is held while the agent runs: authentication
returns its connection before the turn starts, and every agent step opens its own
short-lived one.
"""

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agent import (
    Agent,
    AgentConfigError,
    AgentError,
    AgentInputError,
    AgentUnavailableError,
    MutationStatus,
    ResponsePlan,
    create_agent,
    postgres_checkpointer,
    thread_id_for,
)
from app.agent.datasets import ChatData
from app.agent.graph import MAX_MESSAGE_LENGTH
from app.auth.dependencies import get_current_user_released
from app.auth.permissions import AuthenticatedUser, PermissionDenied
from app.services import BusinessError
from app.services.errors import ConflictError, InsufficientStockError, NotFoundError, OperationFailedError, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# How long a message waits for an earlier turn in the same conversation before it is refused.
BUSY_WAIT_SECONDS = 5.0

_UNAVAILABLE = "The assistant is temporarily unavailable; please try again"
_INTERNAL = "Something went wrong while answering; please try again"
_BUSINESS_STATUS = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InsufficientStockError, status.HTTP_409_CONFLICT),
    (ConflictError, status.HTTP_409_CONFLICT),
    (OperationFailedError, status.HTTP_503_SERVICE_UNAVAILABLE),
)


class ChatRequest(BaseModel):
    """Only the conversation and the message: identity and role come from the access token."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH)]


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message: str  # the assistant's reply
    plan: ResponsePlan  # Layer 7: intent, source and how to present the answer
    # Layer 8: the change this turn proposed, executed, cancelled or refused; null when none.
    # state "confirmation_required" means a change is waiting for the user's "confirm" or "cancel".
    mutation: MutationStatus | None
    # Layer 14: bounded, allowlisted rows and figures behind plan.datasets, from this turn's tool results.
    data: ChatData


# --- agent lifecycle -----------------------------------------------------------------


class AgentRuntime:
    """The process's agent and its checkpointer pool, created on first use and closed on shutdown.

    Lazy, so the API (health, login) starts even when the model or database is not configured.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stack = ExitStack()
        self._agent: Agent | None = None

    def get(self) -> Agent:
        with self._lock:
            if self._agent is None:
                with ExitStack() as stack:
                    checkpointer = stack.enter_context(postgres_checkpointer())
                    self._agent = create_agent(checkpointer)
                    self._stack = stack.pop_all()  # keep the pool open only if the agent was built
            return self._agent

    def close(self) -> None:
        with self._lock:
            self._agent = None
            self._stack.close()


agent_runtime = AgentRuntime()


def get_agent() -> Agent:
    try:
        return agent_runtime.get()
    except AgentConfigError as exc:
        # The reason (missing key, unreachable storage, schema mismatch) is for operators, not clients.
        logger.error("Agent is not available: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None


# --- one turn at a time per conversation -----------------------------------------------


class ConversationBusy(Exception):
    pass


class ConversationLocks:
    """Serializes turns of the same conversation thread.

    Process-local: correct for a single API process. Several processes would need a shared
    lock (for example a PostgreSQL advisory lock taken per turn). An entry exists only while
    a turn holds or waits for it, so abandoned conversations leave nothing behind.
    """

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        self._guard = threading.Lock()
        self._entries: dict[str, list] = {}  # thread id -> [lock, holders and waiters]

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._guard:
            entry = self._entries.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            if not entry[0].acquire(timeout=self.wait_seconds):
                raise ConversationBusy
            try:
                yield
            finally:
                entry[0].release()
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._entries[key]

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)


conversation_locks = ConversationLocks(BUSY_WAIT_SECONDS)


# --- endpoint --------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: AuthenticatedUser = Depends(get_current_user_released),
    agent: Agent = Depends(get_agent),
) -> ChatResponse:
    # The same key the agent checkpoints under: always scoped to the authenticated user.
    thread_id = thread_id_for(user, body.conversation_id)
    try:
        with conversation_locks.hold(thread_id):
            reply = agent.respond(user, body.conversation_id, body.message)
    except ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another message in this conversation is still being answered; please wait for it to finish",
        ) from None
    except AgentInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except (AgentUnavailableError, AgentConfigError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None
    except AgentError as exc:
        # Agent messages are written to be shown to users (e.g. the step limit was reached).
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from None
    except PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden") from None
    except BusinessError as exc:
        code = next((code for kind, code in _BUSINESS_STATUS if isinstance(exc, kind)), status.HTTP_400_BAD_REQUEST)
        raise HTTPException(code, str(exc)) from None
    except Exception:
        logger.exception("Chat turn failed for user %s", user.id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _INTERNAL) from None
    return ChatResponse(
        conversation_id=body.conversation_id, message=reply.text, plan=reply.plan, mutation=reply.mutation,
        data=reply.data,
    )
