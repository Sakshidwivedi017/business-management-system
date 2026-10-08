"""LangSmith tracing (Layer 16): optional, privacy-first, passive.

LangGraph and ChatOpenAI already report their runs (graph, nodes, model calls with token
usage) to LangSmith whenever tracing is enabled in the current context, so a chat turn only
needs `traced_turn()` around it. The tool executor and semantic retrieval are plain Python,
so they add their own spans with `traced_step()`.

Privacy: the client hides every run's inputs and outputs, so prompts, user messages, tool
arguments, tool results, rows and documents are never sent. Metadata is reduced to an
allowlist (node names, model and token usage, and the safe facts the spans add); the
conversation thread id, which contains the user id, is dropped. A span that fails records
only the exception type, never its message or traceback.

Configuration comes only from Settings (LANGSMITH_TRACING, LANGSMITH_API_KEY,
LANGSMITH_PROJECT). When tracing is off, nothing is created and nothing is sent, and the
SDK's own environment switch is overridden so it cannot trace unredacted data either.
Upload happens in the SDK's background thread, so an unreachable LangSmith never fails a request.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from langsmith import Client, trace, tracing_context
from langsmith.utils import tracing_is_enabled

from app.config import get_settings

logger = logging.getLogger(__name__)

# Metadata keys sent with a run; everything else (thread_id, checkpoint ids, ...) is dropped.
SAFE_METADATA_PREFIXES = ("langgraph_", "ls_")
SAFE_METADATA_KEYS = frozenset({
    "usage_metadata",  # token counts, added by the LangChain tracer
    "role",
    "tool", "mutation", "outcome", "error_kind", "error_type",
    "source", "top_k", "results", "min_similarity", "similarity_max", "similarity_min",
})


def safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metadata.items() if k in SAFE_METADATA_KEYS or k.startswith(SAFE_METADATA_PREFIXES)}


@lru_cache
def get_langsmith_client() -> Client | None:
    """The redacting LangSmith client, or None when tracing is off or not configured."""
    settings = get_settings()
    if not settings.langsmith_tracing:
        return None
    if settings.langsmith_api_key is None:
        logger.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set; tracing stays off")
        return None
    try:
        return Client(
            api_key=settings.langsmith_api_key.get_secret_value(),
            hide_inputs=True,
            hide_outputs=True,
            hide_metadata=safe_metadata,
            omit_traced_runtime_info=True,
        )
    except Exception as exc:
        logger.warning("LangSmith client could not be created (%s); tracing stays off", type(exc).__name__)
        return None


@contextmanager
def traced_turn(**metadata: Any) -> Iterator[None]:
    """Trace everything run inside (one agent turn) to the configured project, or explicitly nothing."""
    client = get_langsmith_client()
    if client is None:
        with tracing_context(enabled=False):
            yield
        return
    with tracing_context(enabled=True, client=client, project_name=get_settings().langsmith_project,
                         metadata=metadata):
        yield


@contextmanager
def traced_step(name: str, run_type: str, **metadata: Any) -> Iterator[dict[str, Any]]:
    """A child span carrying only metadata. The caller adds safe facts to the yielded dict.

    Inactive (a plain dict, no LangSmith code) unless inside an enabled traced_turn.
    Exceptions are re-raised unchanged; the span records only their type.
    """
    facts = dict(metadata)
    if get_langsmith_client() is None or tracing_is_enabled() is not True:
        yield facts
        return
    try:
        span = trace(name, run_type=run_type, inputs={})
        run = span.__enter__()
    except Exception as exc:  # tracing must never break the traced work
        logger.warning("Tracing span %s could not start: %s", name, type(exc).__name__)
        yield facts
        return
    error_type = None
    try:
        yield facts
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        _finish(span, run, facts, error_type)


def _finish(span: trace, run: Any, facts: dict[str, Any], error_type: str | None) -> None:
    try:
        if error_type is not None:
            facts["error_type"] = error_type
            run.end(error=error_type)  # the type only: messages and tracebacks can carry data
        run.add_metadata({k: v for k, v in facts.items() if v is not None})
    except Exception as exc:
        logger.warning("Tracing span could not be completed: %s", type(exc).__name__)
    finally:
        try:
            span.__exit__(None, None, None)  # sends the run and restores the outer tracing context
        except Exception as exc:
            logger.warning("Tracing span could not be sent: %s", type(exc).__name__)
