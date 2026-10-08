"""Agent exceptions. Their messages are safe to show to end users."""


class AgentError(Exception):
    """Base for agent failures the caller should report, not crash on."""


class AgentConfigError(AgentError):
    """The agent is not configured (missing key, unsupported model, incompatible checkpoint schema)."""


class AgentInputError(AgentError):
    """The caller passed an invalid message or conversation id."""


class AgentUnavailableError(AgentError):
    """The language model could not be reached or failed; nothing was answered."""
