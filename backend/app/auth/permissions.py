"""Role-based permissions: the single source of truth for what each role may do.

Business operations call ensure_permission() directly, so authorization holds
no matter how they are invoked (HTTP route today, agent tool later).
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Role(StrEnum):
    INVENTORY_MANAGER = "inventory_manager"
    PROCUREMENT_MANAGER = "procurement_manager"
    OWNER = "owner"


class Permission(StrEnum):
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
    PROCUREMENT_READ = "procurement:read"
    PROCUREMENT_WRITE = "procurement:write"
    ANALYTICS_READ = "analytics:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.INVENTORY_MANAGER: frozenset({Permission.INVENTORY_READ, Permission.INVENTORY_WRITE}),
    Role.PROCUREMENT_MANAGER: frozenset(
        {Permission.INVENTORY_READ, Permission.PROCUREMENT_READ, Permission.PROCUREMENT_WRITE}
    ),
    Role.OWNER: frozenset(
        {Permission.INVENTORY_READ, Permission.PROCUREMENT_READ, Permission.ANALYTICS_READ}
    ),
}


class AuthenticatedUser(BaseModel):
    """Safe identity of the caller. Role always comes from the current users row."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role


class PermissionDenied(Exception):
    def __init__(self, role: Role, permission: Permission):
        super().__init__(f"Role '{role}' lacks permission '{permission}'")
        self.role = role
        self.permission = permission


def has_permission(user: AuthenticatedUser, permission: Permission | str) -> bool:
    # Permission(...) raises ValueError for unknown names, so typos fail loudly instead of denying silently.
    return Permission(permission) in ROLE_PERMISSIONS.get(user.role, frozenset())


def ensure_permission(user: AuthenticatedUser, permission: Permission | str) -> None:
    if not has_permission(user, permission):
        raise PermissionDenied(user.role, Permission(permission))
