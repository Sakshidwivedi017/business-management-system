import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth import security
from app.auth.security import (
    InvalidTokenError,
    authenticate_user,
    create_access_token,
    decode_access_token,
    verify_password,
)
from app.config import get_settings
from tests.conftest import make_user

PASSWORD = "correct horse battery staple"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _encode(claims: dict, key: str | None = None) -> str:
    return jwt.encode(claims, key or get_settings().jwt_secret_key, algorithm="HS256")


@pytest.fixture
def lookup(monkeypatch):
    users = {}
    monkeypatch.setattr(security, "get_user_by_email", lambda _conn, email: users.get(email))
    return users


# --- passwords -------------------------------------------------------------

def test_correct_password_verifies():
    assert verify_password(PASSWORD, make_user("owner")["password_hash"])


def test_wrong_password_fails():
    assert not verify_password("wrong", make_user("owner")["password_hash"])


def test_corrupt_hash_or_oversized_password_fails_safely():
    assert not verify_password(PASSWORD, "not-a-bcrypt-hash")
    assert not verify_password("x" * 100, make_user("owner")["password_hash"])


def test_authenticate_valid_user(lookup):
    user = make_user("owner")
    lookup[user["email"]] = user
    assert authenticate_user(None, user["email"], PASSWORD) is user


def test_authenticate_wrong_password(lookup):
    user = make_user("owner")
    lookup[user["email"]] = user
    assert authenticate_user(None, user["email"], "wrong") is None


def test_authenticate_inactive_user_with_correct_password(lookup):
    user = make_user("owner", is_active=False)
    lookup[user["email"]] = user
    assert authenticate_user(None, user["email"], PASSWORD) is None


def test_unknown_email_still_runs_dummy_bcrypt_check(lookup, monkeypatch):
    checked = []
    real_verify = security.verify_password
    monkeypatch.setattr(
        security, "verify_password", lambda pw, h: checked.append(h) or real_verify(pw, h)
    )
    assert authenticate_user(None, "nobody@test.local", PASSWORD) is None
    assert checked == [security._DUMMY_HASH]


# --- tokens ----------------------------------------------------------------

def test_token_round_trip_contains_only_minimal_claims():
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, 3)
    assert decode_access_token(token) == (user_id, 3)
    assert expires_in == 30 * 60
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"sub", "ver", "exp"}


def test_expired_token_rejected():
    token = _encode({"sub": str(uuid.uuid4()), "ver": 0, "exp": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tampered_payload_rejected():
    token, _ = create_access_token(uuid.uuid4(), 0)
    header, _, signature = token.split(".")
    forged = _b64({"sub": str(uuid.uuid4()), "ver": 0, "exp": 9999999999})
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{forged}.{signature}")


def test_token_signed_with_other_secret_rejected():
    token = _encode({"sub": str(uuid.uuid4()), "ver": 0, "exp": 9999999999}, key="x" * 48)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_alg_none_token_rejected():
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': str(uuid.uuid4()), 'ver': 0, 'exp': 9999999999})}."
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "not.a.jwt.token", "Bearer x"])
def test_malformed_token_rejected(token):
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


@pytest.mark.parametrize(
    "claims",
    [
        {"ver": 0, "exp": 9999999999},  # no user id
        {"sub": str(uuid.uuid4()), "exp": 9999999999},  # no token_version
        {"sub": str(uuid.uuid4()), "ver": 0},  # no expiry
        {"sub": "not-a-uuid", "ver": 0, "exp": 9999999999},
        {"sub": str(uuid.uuid4()), "ver": "0", "exp": 9999999999},
        {"sub": str(uuid.uuid4()), "ver": True, "exp": 9999999999},
    ],
    ids=["missing-sub", "missing-ver", "missing-exp", "bad-sub", "string-ver", "bool-ver"],
)
def test_token_with_missing_or_invalid_claims_rejected(claims):
    with pytest.raises(InvalidTokenError):
        decode_access_token(_encode(claims))
