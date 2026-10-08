"""Read-only data access for the users table.

Rows include password_hash and token_version for the future auth layer;
callers must never return them to clients.
"""

import uuid
from typing import Any

from sqlalchemy import Connection

from app.db.connection import fetch_one

_USER_SELECT = """
    SELECT id, email, password_hash, full_name, role, is_active, token_version,
           created_by, created_at, updated_at
    FROM users
"""


def get_user_by_id(conn: Connection, user_id: str | uuid.UUID) -> dict[str, Any] | None:
    # users.id is a uuid column; a malformed id cannot match any row.
    try:
        user_uuid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    return fetch_one(conn, _USER_SELECT + " WHERE id = :user_id", user_id=user_uuid)


def get_user_by_email(conn: Connection, email: str) -> dict[str, Any] | None:
    # Emails are stored lowercase; normalise input to use the indexed column directly.
    return fetch_one(conn, _USER_SELECT + " WHERE email = :email", email=email.strip().lower())
