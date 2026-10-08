import uuid

import pytest

from app.auth.permissions import (
    AuthenticatedUser,
    Permission,
    PermissionDenied,
    Role,
    ensure_permission,
    has_permission,
)

EXPECTED = {
    Role.INVENTORY_MANAGER: {
        Permission.INVENTORY_READ: True,
        Permission.INVENTORY_WRITE: True,
        Permission.PROCUREMENT_READ: False,
        Permission.PROCUREMENT_WRITE: False,
        Permission.ANALYTICS_READ: False,
    },
    Role.PROCUREMENT_MANAGER: {
        Permission.INVENTORY_READ: True,
        Permission.INVENTORY_WRITE: False,
        Permission.PROCUREMENT_READ: True,
        Permission.PROCUREMENT_WRITE: True,
        Permission.ANALYTICS_READ: False,
    },
    Role.OWNER: {
        Permission.INVENTORY_READ: True,
        Permission.INVENTORY_WRITE: False,
        Permission.PROCUREMENT_READ: True,
        Permission.PROCUREMENT_WRITE: False,
        Permission.ANALYTICS_READ: True,
    },
}

MATRIX = [(role, perm, allowed) for role, perms in EXPECTED.items() for perm, allowed in perms.items()]


def user_with(role: Role) -> AuthenticatedUser:
    return AuthenticatedUser(id=uuid.uuid4(), email="x@test.local", full_name="X", role=role)


def test_matrix_covers_every_role_and_permission():
    assert len(MATRIX) == len(Role) * len(Permission) == 15


@pytest.mark.parametrize(("role", "permission", "allowed"), MATRIX, ids=lambda v: str(v))
def test_role_permission_matrix(role, permission, allowed):
    user = user_with(role)
    assert has_permission(user, permission) is allowed
    # Plain strings, as Layer 4 may pass them, behave identically.
    assert has_permission(user, permission.value) is allowed
    if allowed:
        ensure_permission(user, permission)
    else:
        with pytest.raises(PermissionDenied):
            ensure_permission(user, permission)


def test_unknown_permission_is_an_error_not_a_silent_deny():
    with pytest.raises(ValueError):
        ensure_permission(user_with(Role.OWNER), "inventory:delete")


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        user_with("admin")


def test_authenticated_user_is_immutable():
    user = user_with(Role.INVENTORY_MANAGER)
    with pytest.raises(Exception):
        user.role = Role.OWNER
