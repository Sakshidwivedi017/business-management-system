"""Password verification, login and JWT access tokens.

Tokens carry only the user id (sub), token_version (ver) and expiry (exp).
Everything else, including the role, is reloaded from the database per request.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from sqlalchemy import Connection

from app.config import get_settings
from app.db.repositories.users import get_user_by_email

ALGORITHM = "HS256"

# Verified against when the email is unknown, so both failure paths cost one bcrypt check.
# Same cost factor (12) as the stored hashes.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt(rounds=12))


class InvalidTokenError(Exception):
    """Token is missing required claims, malformed, expired or wrongly signed."""


def verify_password(password: str, password_hash: str | bytes) -> bool:
    if isinstance(password_hash, str):
        password_hash = password_hash.encode()
    try:
        return bcrypt.checkpw(password.encode(), password_hash)
    except ValueError:
        # Corrupt stored hash, or a password over bcrypt's 72-byte limit.
        return False


def authenticate_user(conn: Connection, email: str, password: str) -> dict[str, Any] | None:
    """Return the user row for valid credentials of an active user, else None.

    Callers must not distinguish the failure reasons to the client.
    """
    user = get_user_by_email(conn, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    if not user["is_active"]:
        return None
    return user


def create_access_token(user_id: uuid.UUID | str, token_version: int) -> tuple[str, int]:
    """Return (token, lifetime in seconds)."""
    settings = get_settings()
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    claims = {
        "sub": str(user_id),
        "ver": token_version,
        "exp": datetime.now(UTC) + lifetime,
    }
    token = jwt.encode(claims, settings.jwt_secret_key, algorithm=ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> tuple[uuid.UUID, int]:
    """Validate signature, expiry and claims; return (user_id, token_version)."""
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "ver", "exp"]},
        )
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
        raise InvalidTokenError from exc
    version = claims["ver"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidTokenError
    return user_id, version
