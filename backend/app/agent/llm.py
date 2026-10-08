"""OpenAI chat model for the agent. The key stays server-side and is never logged."""

from langchain_openai import ChatOpenAI

from app.agent.errors import AgentConfigError
from app.config import get_settings

REQUEST_TIMEOUT_SECONDS = 60


def create_chat_model() -> ChatOpenAI:
    settings = get_settings()
    if settings.openai_api_key is None:
        raise AgentConfigError("OPENAI_API_KEY is not set in backend/.env")
    if settings.agent_model is None:
        raise AgentConfigError("AGENT_MODEL is not set in backend/.env")
    model = ChatOpenAI(
        model=settings.agent_model,
        api_key=settings.openai_api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=2,
    )
    # langchain-openai ships a capability profile per known model; unknown names have none.
    if not (model.profile or {}).get("tool_calling"):
        raise AgentConfigError(
            f"AGENT_MODEL '{settings.agent_model}' is not a tool-calling model known to langchain-openai"
        )
    return model
