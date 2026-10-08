"""Semantic search: validation, permissions and vector ranking against the live index (rolled back)."""

import uuid

import pytest

from app.auth.permissions import ROLE_PERMISSIONS, AuthenticatedUser, PermissionDenied, Role
from app.db.repositories import knowledge as repo
from app.rag import search as rag_search
from app.rag.search import NO_RESULTS_MESSAGE, search_knowledge
from app.services import ValidationError
from tests.fakes import FakeEmbedder, make_agent_user

USER = make_agent_user(Role.INVENTORY_MANAGER)


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder(vectors={"plate": [1.0, 0.0, 0.0], "unrelated": [0.0, -1.0, 0.0]})
    monkeypatch.setattr(rag_search, "get_embedder", lambda: fake)
    return fake


@pytest.fixture
def indexed(conn):
    """Documents with hand-picked vectors under a test model, so similarities are exact."""
    if not repo.index_exists(conn):
        pytest.skip("semantic index not created (python -m app.rag.index --create-schema)")
    docs = {
        "exact": ([1.0, 0.0, 0.0], "item"),            # similarity 1.0
        "close": ([1.0, 1.0, 0.0], "item_classification"),  # ~0.707
        "weak": ([1.0, 0.0, 3.0], "item"),            # ~0.316
        "orthogonal": ([0.0, 1.0, 0.0], "item"),      # 0.0
        "opposite": ([-1.0, 0.0, 0.0], "item"),       # -1.0
    }
    for name, (vector, source) in docs.items():
        repo.upsert_document(
            conn, source=source, source_id=f"t-{name}", title=f"Title {name}", content=f"Content {name}",
            metadata={"name": name}, content_hash=name, embedding_model="test-model", embedding=vector,
        )
    return conn


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": 42},
        {"query": "plate", "top_k": 0},
        {"query": "plate", "top_k": 11},
        {"query": "plate", "top_k": True},
        {"query": "plate", "top_k": "5"},
        {"query": "plate", "source": "users"},
    ],
)
def test_invalid_input_rejected_before_embedding(kwargs, embedder):
    with pytest.raises(ValidationError):
        search_knowledge(None, USER, **kwargs)
    assert embedder.calls == []


def test_permission_checked_first(monkeypatch, embedder):
    monkeypatch.setitem(ROLE_PERMISSIONS, Role.INVENTORY_MANAGER, frozenset())
    with pytest.raises(PermissionDenied):
        search_knowledge(None, USER, "plate")
    assert embedder.calls == []


@pytest.mark.parametrize("role", list(Role))
def test_every_role_may_search(role, indexed, embedder):
    user = AuthenticatedUser(id=uuid.uuid4(), email="x@test.local", full_name="X", role=role)
    assert search_knowledge(indexed, user, "plate")["results"]


def test_ranked_results_above_threshold(indexed, embedder):
    result = search_knowledge(indexed, USER, "plate", top_k=10)
    assert embedder.calls == [["plate"]]  # exactly one embedding request per search
    assert [r["source_id"] for r in result["results"]] == ["t-exact", "t-close", "t-weak"]
    assert [r["similarity"] for r in result["results"]] == [1.0, 0.707, 0.316]
    assert result["min_similarity"] == 0.3 and "message" not in result
    top = result["results"][0]
    assert top == {
        "source": "item", "source_id": "t-exact", "title": "Title exact", "similarity": 1.0,
        "content": "Content exact", "metadata": {"name": "exact"},
    }


def test_top_k_and_source_filter(indexed, embedder):
    assert [r["source_id"] for r in search_knowledge(indexed, USER, "plate", top_k=1)["results"]] == ["t-exact"]
    filtered = search_knowledge(indexed, USER, "plate", source="item_classification")["results"]
    assert [r["source_id"] for r in filtered] == ["t-close"]


def test_no_relevant_results(indexed, embedder):
    result = search_knowledge(indexed, USER, "unrelated")
    assert result["results"] == [] and result["message"] == NO_RESULTS_MESSAGE


def test_other_model_vectors_are_ignored(indexed, embedder):
    # Real index rows use another model (and dimension); only same-model rows are compared.
    embedder.model = "another-model"
    assert search_knowledge(indexed, USER, "plate")["results"] == []
