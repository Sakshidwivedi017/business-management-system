"""Business exceptions. Their messages are safe to show to end users.

Unexpected database failures are logged server-side and re-raised as
OperationFailedError, so SQL, parameters and driver details never escape.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

logger = logging.getLogger(__name__)


class BusinessError(Exception):
    """Base for expected, user-presentable failures."""


class NotFoundError(BusinessError):
    pass


class ValidationError(BusinessError):
    pass


class InsufficientStockError(BusinessError):
    pass


class ConflictError(BusinessError):
    pass


class OperationFailedError(BusinessError):
    pass


@contextmanager
def database_errors(operation: str) -> Iterator[None]:
    """Translate driver errors into sanitized business errors. Usable as a decorator."""
    try:
        yield
    except IntegrityError as exc:
        logger.warning("%s rejected by a database constraint (%s)", operation, _sqlstate(exc))
        raise ConflictError("The change conflicts with existing data; nothing was saved") from None
    except SQLAlchemyError as exc:
        logger.error("%s failed: %s (%s)", operation, type(exc).__name__, _sqlstate(exc))
        raise OperationFailedError("The operation could not be completed; nothing was saved") from None


def _sqlstate(exc: SQLAlchemyError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)
