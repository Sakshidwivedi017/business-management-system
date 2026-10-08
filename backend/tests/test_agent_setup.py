"""Agent configuration: the OpenAI model factory and the checkpoint schema guard (no network, no writes)."""

from contextlib import contextmanager

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import SecretStr

from app.agent import checkpoint, llm
from app.agent.errors import AgentConfigError
from app.config import Settings, get_settings

FAKE_KEY = "sk-test-not-a-real-key-0123456789"


def use_settings(monkeypatch, **overrides):
    monkeypatch.setattr(llm, "get_settings", lambda: get_settings().model_copy(update=overrides))


def test_missing_key_or_model_refused(monkeypatch):
    use_settings(monkeypatch, openai_api_key=None, agent_model="gpt-5.4-mini")
    with pytest.raises(AgentConfigError, match="OPENAI_API_KEY"):
        llm.create_chat_model()
    use_settings(monkeypatch, openai_api_key=SecretStr(FAKE_KEY), agent_model=None)
    with pytest.raises(AgentConfigError, match="AGENT_MODEL"):
        llm.create_chat_model()


def test_unknown_model_refused(monkeypatch):
    use_settings(monkeypatch, openai_api_key=SecretStr(FAKE_KEY), agent_model="gpt-imaginary-9")
    with pytest.raises(AgentConfigError, match="not a tool-calling model"):
        llm.create_chat_model()


def test_tool_calling_model_accepted_and_key_hidden(monkeypatch):
    use_settings(monkeypatch, openai_api_key=SecretStr(FAKE_KEY), agent_model="gpt-5.4-mini")
    model = llm.create_chat_model()
    assert model.model_name == "gpt-5.4-mini"
    assert FAKE_KEY not in repr(model) and FAKE_KEY not in str(model.model_dump())


def test_settings_blank_agent_values_are_unset():
    settings = Settings(database_url="postgresql://u:p@h/d", jwt_secret_key="x" * 32, openai_api_key=" ", agent_model="")
    assert settings.openai_api_key is None and settings.agent_model is None
    assert settings.agent_max_iterations == 12
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://u:p@h/d", jwt_secret_key="x" * 32, agent_max_iterations=0)


def test_installed_checkpointer_matches_existing_schema():
    # The existing tables record migrations 0-9; this package version must expect exactly those.
    assert len(PostgresSaver.MIGRATIONS) == 10
    assert checkpoint.EXPECTED_SCHEMA_VERSION == 9


class FakePool:
    def __init__(self, version=None, error=None):
        self.version, self.error = version, error

    @contextmanager
    def connection(self):
        if self.error:
            raise self.error
        yield self

    def execute(self, sql):
        assert sql == "SELECT max(v) AS v FROM checkpoint_migrations"
        return self

    def fetchone(self):
        return {"v": self.version}


@pytest.mark.parametrize("version", [None, 8, 10])
def test_schema_guard_refuses_other_versions(version):
    with pytest.raises(AgentConfigError, match="refusing to migrate"):
        checkpoint.verify_schema_version(FakePool(version))


def test_schema_guard_hides_connection_errors():
    with pytest.raises(AgentConfigError, match="^Conversation storage is unavailable$"):
        checkpoint.verify_schema_version(FakePool(error=psycopg.OperationalError("password=hunter2")))


def test_schema_guard_accepts_expected_version():
    checkpoint.verify_schema_version(FakePool(9))


def test_live_checkpoint_schema_is_compatible(live_db):
    # Read-only: opens the pool and checks checkpoint_migrations; never calls setup().
    with checkpoint.postgres_checkpointer(max_size=1) as saver:
        assert isinstance(saver, PostgresSaver)
