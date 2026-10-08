from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    app_name: str = "lecxe-chatbot"
    app_env: str = "development"
    log_level: str = "INFO"
    # Comma-separated list of allowed frontend origins.
    cors_origins: str = "http://localhost:3000"
    database_url: str
    # Signs JWT access tokens. Required; never exposed to clients or logs.
    jwt_secret_key: str
    access_token_expire_minutes: int = 30
    # Agent (Layer 5). Optional so the API runs without them; the agent refuses to start if unset.
    openai_api_key: SecretStr | None = None
    agent_model: str | None = None
    # Model calls allowed per user message before the agent stops with a bounded reply.
    agent_max_iterations: int = 12
    # Semantic retrieval (Layer 6). Uses the same OPENAI_API_KEY.
    embedding_model: str | None = None
    # Minimum cosine similarity (0-1) for a semantic result to be returned.
    rag_similarity_threshold: float = 0.3
    # LangSmith tracing (Layer 16). Off by default; inputs and outputs are never sent. The SDK's own
    # environment-based switch is overridden, so these settings alone decide whether anything is traced.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "lecxe-chatbot"

    @field_validator("langsmith_project", mode="before")
    @classmethod
    def default_project(cls, value):
        return "lecxe-chatbot" if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator("openai_api_key", "agent_model", "embedding_model", "langsmith_api_key", mode="before")
    @classmethod
    def blank_as_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("agent_max_iterations")
    @classmethod
    def bounded_iterations(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("AGENT_MAX_ITERATIONS must be between 1 and 50")
        return value

    @field_validator("rag_similarity_threshold")
    @classmethod
    def bounded_threshold(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("RAG_SIMILARITY_THRESHOLD must be between 0 and 1")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def require_strong_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return value

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, value: str) -> str:
        # Accept plain postgres URLs and route them to the installed psycopg 3 driver.
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
