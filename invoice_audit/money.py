"""Money and quantity arithmetic.

Every monetary value in this package is a ``Decimal``. Floats are never used:
a flag is an assertion we may have to defend to a warehouse inside a dispute
window, and binary floating point cannot represent $0.10 exactly.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: object) -> Decimal:
    """Coerce to a Decimal rounded to cents.

    Accepts str, int, Decimal. Rejects float outright rather than silently
    inheriting its representation error.
    """
    if isinstance(value, float):
        raise TypeError(
            "refusing to build money from a float; pass a str or Decimal instead"
        )
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a monetary value: {value!r}") from exc


def quantity(value: object) -> Decimal:
    """Coerce to a Decimal quantity.

    Quantities are not rounded to cents: a rate card may price per cubic foot
    and bill 12.375 of them.
    """
    if isinstance(value, float):
        raise TypeError(
            "refusing to build a quantity from a float; pass a str or Decimal"
        )
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a quantity: {value!r}") from exc


def fmt(value: Decimal, currency: str = "USD") -> str:
    """Render money for a human reading the exception queue."""
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, f"{currency} ")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def pct(value: Decimal) -> str:
    """Render a percentage without trailing noise (5 -> '5%', 7.5 -> '7.5%')."""
    normalised = value.normalize()
    text = format(normalised, "f")
    return f"{text}%"
