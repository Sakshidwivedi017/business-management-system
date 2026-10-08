"""Deterministic business operations (Layer 4).

Each operation is `op(conn, user, ...)`: it checks the user's permission via
app.auth.ensure_permission before any database access, validates input, and
raises a BusinessError (or PermissionDenied) with a user-safe message.

Writes are atomic. Given a connection with no open transaction they commit on
success; inside a caller's transaction they use a savepoint and the caller
commits, e.g. `with get_engine().begin() as conn: record_stock_movement(conn, ...)`.
"""

from app.auth.permissions import PermissionDenied
from app.services.errors import (
    BusinessError,
    ConflictError,
    InsufficientStockError,
    NotFoundError,
    OperationFailedError,
    ValidationError,
)

__all__ = [
    "BusinessError",
    "ConflictError",
    "InsufficientStockError",
    "NotFoundError",
    "OperationFailedError",
    "PermissionDenied",
    "ValidationError",
]
