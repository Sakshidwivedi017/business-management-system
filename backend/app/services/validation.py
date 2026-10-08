"""Input coercion shared by business operations. Raises ValidationError with safe messages."""

from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.errors import ValidationError

MAX_TEXT_ID = 100
MAX_LIMIT = 200


def require_text(value: Any, field: str, max_length: int = MAX_TEXT_ID) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field} must be at most {max_length} characters")
    return value


def optional_text(value: Any, field: str, max_length: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return require_text(value, field, max_length)


def to_decimal(
    value: Any,
    field: str,
    *,
    places: int,
    minimum: Decimal,
    maximum: Decimal,
    allow_minimum: bool = True,
) -> Decimal:
    """Parse an exact decimal; floats go through str() so 0.1 stays 0.1."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValidationError(f"{field} must be a number")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValidationError(f"{field} must be a number") from None
    if not number.is_finite():
        raise ValidationError(f"{field} must be a finite number")
    if number < minimum or (number == minimum and not allow_minimum):
        comparison = "at least" if allow_minimum else "greater than"
        raise ValidationError(f"{field} must be {comparison} {minimum}")
    if number > maximum:
        raise ValidationError(f"{field} must be at most {maximum}")
    quantum = Decimal(1).scaleb(-places)
    if number != number.quantize(quantum):
        raise ValidationError(f"{field} allows at most {places} decimal places")
    return number.quantize(quantum)


def check_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return limit
