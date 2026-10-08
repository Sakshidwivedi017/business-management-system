"""FastAPI dependencies resolving the caller's identity and enforcing permissions."""

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection

from app.auth.permissions import (
    AuthenticatedUser,
    Permission,
    PermissionDenied,
    Role,
    ensure_permission,
)
from app.auth.security import InvalidTokenError, decode_access_token
from app.db.connection import get_connection, get_engine
from app.db.repositories.users import get_user_by_id

_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    # One generic response for every failure, so callers learn nothing about why.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def to_authenticated_user(row: dict) -> AuthenticatedUser:
    """Strip a users row down to the safe identity fields."""
    return AuthenticatedUser(
        id=row["id"], email=row["email"], full_name=row["full_name"], role=Role(row["role"])
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: Connection = Depends(get_connection),
) -> AuthenticatedUser:
    if credentials is None:
        raise _unauthorized()
    try:
        user_id, token_version = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _unauthorized() from None

    user = get_user_by_id(conn, user_id)
    if user is None or not user["is_active"] or user["token_version"] != token_version:
        raise _unauthorized()
    return to_authenticated_user(user)


def get_current_user_released(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedUser:
    """get_current_user on a connection returned to the pool before the endpoint runs.

    For long requests (an agent turn): the request-scoped get_connection would otherwise
    stay checked out, with the lookup's transaction open, until the response is sent.
    """
    if credentials is None:
        raise _unauthorized()
    with get_engine().connect() as conn:
        return get_current_user(credentials, conn)


def require_permission(permission: Permission) -> Callable[..., AuthenticatedUser]:
    """Dependency factory: `user = Depends(require_permission(Permission.INVENTORY_READ))`."""

    def dependency(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        try:
            ensure_permission(user, permission)
        except PermissionDenied:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from None
        return user

    return dependency
