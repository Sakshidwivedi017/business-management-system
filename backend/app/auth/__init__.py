"""Authentication (who is calling) and RBAC (what they may do)."""

from app.auth.dependencies import get_current_user, require_permission
from app.auth.permissions import (
    AuthenticatedUser,
    Permission,
    PermissionDenied,
    Role,
    ensure_permission,
    has_permission,
)

__all__ = [
    "AuthenticatedUser",
    "Permission",
    "PermissionDenied",
    "Role",
    "ensure_permission",
    "get_current_user",
    "has_permission",
    "require_permission",
]
