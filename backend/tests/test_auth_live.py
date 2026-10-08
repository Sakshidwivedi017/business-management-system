"""Auth flow against the live database. Read-only: no user, password or token rows change."""

import os
import uuid

import pytest

from app.auth.security import create_access_token
from app.db.connection import get_engine
from app.db.repositories.users import get_user_by_email
from tests.conftest import DEMO_EMAILS, assert_no_sensitive_data

pytestmark = pytest.mark.usefixtures("live_db")


def db_user(email: str) -> dict:
    with get_engine().connect() as conn:
        user = get_user_by_email(conn, email)
    assert user is not None, f"expected demo user {email} in database"
    return user


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_unknown_email_rejected(client):
    response = client.post(
        "/api/auth/login", json={"email": f"nobody-{uuid.uuid4().hex}@example.com", "password": "x"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}


@pytest.mark.parametrize("email", DEMO_EMAILS.values())
def test_wrong_password_for_real_user_rejected(client, email):
    response = client.post("/api/auth/login", json={"email": email, "password": f"wrong-{uuid.uuid4().hex}"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}
    assert_no_sensitive_data(response.text)


@pytest.mark.parametrize(("role", "email"), DEMO_EMAILS.items())
def test_me_returns_role_from_database(client, role, email):
    user = db_user(email)
    token, _ = create_access_token(user["id"], user["token_version"])
    response = client.get("/api/auth/me", headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {
        "id": str(user["id"]),
        "email": email,
        "full_name": user["full_name"],
        "role": role,
    }
    assert_no_sensitive_data(response.text)


def test_me_token_version_mismatch_for_real_user(client):
    user = db_user(DEMO_EMAILS["owner"])
    token, _ = create_access_token(user["id"], user["token_version"] + 1)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_me_unknown_user_id(client):
    token, _ = create_access_token(uuid.uuid4(), 0)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("LIVE_LOGIN_EMAIL") and os.getenv("LIVE_LOGIN_PASSWORD")),
    reason="LIVE_LOGIN_EMAIL / LIVE_LOGIN_PASSWORD not set; live successful login not testable",
)
def test_live_successful_login(client):
    response = client.post(
        "/api/auth/login",
        json={"email": os.environ["LIVE_LOGIN_EMAIL"], "password": os.environ["LIVE_LOGIN_PASSWORD"]},
    )
    assert response.status_code == 200
    assert_no_sensitive_data(response.text)
    me = client.get("/api/auth/me", headers=bearer(response.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["role"] == response.json()["user"]["role"]
