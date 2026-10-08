"""HTTP auth flow with user lookups mocked in memory (no database access)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import AuthenticatedUser, Permission, require_permission
from app.auth.security import create_access_token
from app.config import get_settings
from app.db.connection import get_connection
from tests.conftest import assert_no_sensitive_data, make_user
from tests.test_permissions import MATRIX

PASSWORD = "correct horse battery staple"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(user: dict) -> str:
    return create_access_token(user["id"], user["token_version"])[0]


# --- POST /api/auth/login --------------------------------------------------

@pytest.mark.parametrize("role", ["inventory_manager", "procurement_manager", "owner"])
def test_login_valid_credentials(client, fake_users, role):
    user = fake_users(make_user(role))
    response = client.post("/api/auth/login", json={"email": user["email"].upper(), "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert body["user"] == {
        "id": str(user["id"]),
        "email": user["email"],
        "full_name": user["full_name"],
        "role": role,
    }
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert_no_sensitive_data(response.text)

    # The issued token works against /me.
    me = client.get("/api/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["role"] == role


def test_wrong_password_and_unknown_email_are_indistinguishable(client, fake_users):
    user = fake_users(make_user("owner"))
    wrong = client.post("/api/auth/login", json={"email": user["email"], "password": "wrong"})
    unknown = client.post("/api/auth/login", json={"email": "nobody@test.local", "password": PASSWORD})

    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid email or password"}
        assert_no_sensitive_data(response.text)
    assert wrong.headers.get("www-authenticate") == unknown.headers.get("www-authenticate")


def test_login_inactive_user_gets_generic_failure(client, fake_users):
    user = fake_users(make_user("owner", is_active=False))
    response = client.post("/api/auth/login", json={"email": user["email"], "password": PASSWORD})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}


def test_login_ignores_client_supplied_role(client, fake_users):
    user = fake_users(make_user("inventory_manager"))
    response = client.post(
        "/api/auth/login", json={"email": user["email"], "password": PASSWORD, "role": "owner"}
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "inventory_manager"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "a@b.c"},
        {"password": "x"},
        {"email": "", "password": ""},
        {"email": 123, "password": ["x"]},
        {"email": "a@b.c", "password": "x" * 1000},
        "not-json-object",
    ],
)
def test_login_malformed_request(client, fake_users, payload):
    response = client.post("/api/auth/login", json=payload)
    assert response.status_code == 422
    assert_no_sensitive_data(response.text)


def test_login_non_json_body(client, fake_users):
    response = client.post("/api/auth/login", content=b"\x00garbage", headers={"Content-Type": "application/json"})
    assert response.status_code == 422


# --- GET /api/auth/me ------------------------------------------------------

def test_me_valid_token(client, fake_users):
    user = fake_users(make_user("procurement_manager"))
    response = client.get("/api/auth/me", headers=bearer(token_for(user)))
    assert response.status_code == 200
    assert response.json() == {
        "id": str(user["id"]),
        "email": user["email"],
        "full_name": user["full_name"],
        "role": "procurement_manager",
    }
    assert_no_sensitive_data(response.text)


def test_me_role_comes_from_database_not_token(client, fake_users):
    user = fake_users(make_user("inventory_manager"))
    token = jwt.encode(
        {"sub": str(user["id"]), "ver": 0, "exp": datetime.now(UTC) + timedelta(minutes=5), "role": "owner"},
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )
    assert client.get("/api/auth/me", headers=bearer(token)).json()["role"] == "inventory_manager"

    # A role change in the database takes effect on the very next request.
    user["role"] = "owner"
    assert client.get("/api/auth/me", headers=bearer(token)).json()["role"] == "owner"


def _assert_unauthorized(response):
    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert_no_sensitive_data(response.text)


def test_me_missing_token(client, fake_users):
    _assert_unauthorized(client.get("/api/auth/me"))


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer ", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Token abc", "bearer a.b.c"],
)
def test_me_invalid_token(client, fake_users, header):
    _assert_unauthorized(client.get("/api/auth/me", headers={"Authorization": header}))


def test_me_expired_token(client, fake_users):
    user = fake_users(make_user("owner"))
    token = jwt.encode(
        {"sub": str(user["id"]), "ver": 0, "exp": datetime.now(UTC) - timedelta(seconds=1)},
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )
    _assert_unauthorized(client.get("/api/auth/me", headers=bearer(token)))


def test_me_token_version_mismatch(client, fake_users):
    user = fake_users(make_user("owner", token_version=2))
    stale = create_access_token(user["id"], 1)[0]
    _assert_unauthorized(client.get("/api/auth/me", headers=bearer(stale)))


def test_me_inactive_user(client, fake_users):
    user = fake_users(make_user("owner"))
    token = token_for(user)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 200

    # Deactivation revokes access immediately, even with an unexpired token.
    user["is_active"] = False
    _assert_unauthorized(client.get("/api/auth/me", headers=bearer(token)))


def test_me_deleted_user(client, fake_users):
    token = create_access_token(uuid.uuid4(), 0)[0]
    _assert_unauthorized(client.get("/api/auth/me", headers=bearer(token)))


# --- require_permission over HTTP ------------------------------------------

@pytest.fixture
def rbac_client(fake_users):
    """Throwaway app with one route per permission; not part of the real API."""
    rbac_app = FastAPI()
    rbac_app.dependency_overrides[get_connection] = lambda: None
    for permission in Permission:

        @rbac_app.get(f"/check/{permission.value}")
        def check(user: AuthenticatedUser = Depends(require_permission(permission))):
            return {"role": user.role}

    with TestClient(rbac_app) as test_client:
        yield test_client, fake_users


@pytest.mark.parametrize(("role", "permission", "allowed"), MATRIX, ids=lambda v: str(v))
def test_require_permission_over_http(rbac_client, role, permission, allowed):
    test_client, add_user = rbac_client
    user = add_user(make_user(role.value))
    response = test_client.get(f"/check/{permission.value}", headers=bearer(token_for(user)))
    if allowed:
        assert response.status_code == 200
    else:
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}


def test_require_permission_unauthenticated_is_401_not_403(rbac_client):
    test_client, _ = rbac_client
    assert test_client.get("/check/inventory:read").status_code == 401
