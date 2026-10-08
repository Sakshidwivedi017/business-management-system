"""Test doubles for the agent: a scripted chat model and an engine that must not be used."""

import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.auth.permissions import AuthenticatedUser, Role


class ScriptedChatModel(BaseChatModel):
    """Replays prepared AIMessages and records what the agent sent and which tools it bound."""

    responses: list[Any] = Field(default_factory=list)  # AIMessage, or an Exception to raise
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[list[str]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append([tool["function"]["name"] for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


def tool_call(name: str, args: dict, call_id: str | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id or f"call_{uuid.uuid4().hex[:8]}"}])


def make_agent_user(role: Role) -> AuthenticatedUser:
    return AuthenticatedUser(id=uuid.uuid4(), email=f"{role}@test.local", full_name=f"Test {role}", role=role)


class FakeEmbedder:
    """Deterministic embeddings: a fixed vector per known text, `default` otherwise. Can be told to fail."""

    def __init__(self, vectors=None, default=(1.0, 0.0, 0.0), model="test-model", error=None):
        self.vectors = vectors or {}
        self.default = list(default)
        self.model = model
        self.error = error
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return [list(self.vectors.get(t, self.default)) for t in texts]


class NoDatabase:
    """Stands in for the engine where a code path must not reach the database."""

    def connect(self):
        raise AssertionError("the database must not be touched")
