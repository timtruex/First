"""
Fixed-point primitives for cross-venue binary contracts.

Kalshi and Polymarket quote the same thing in different numeric domains, and
the difference is not cosmetic:

    Kalshi      whole contracts, integer cent prices 1..99, $1.00 settlement
    Polymarket  fractional shares, decimal prices 0..1 with a 1e-2/1e-3 tick

A cross-venue spread is only meaningful once both are on one scale, and the
conversion must be exact — a spread is a difference of two nearly equal
numbers, which is precisely the arithmetic where float error stops being
academic. A 0.4c edge is a real trade; it is also the same order of magnitude
as the error from accumulating float conversions across two order books.

Canonical internal scale is **probability in centi-cents**: an integer 1..9999
where 5000 == $0.50 == 50c. Kalshi cents multiply in exactly (50c -> 5000).
Polymarket decimals quantise to the same grid. Contract counts are integers on
Kalshi and Decimal shares on Polymarket, so they are kept distinct types rather
than silently unified.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, InvalidOperation
from typing import Final

__all__ = [
    "ZERO", "ONE", "CENT", "CENTI", "DOLLAR",
    "MIN_PROB", "MAX_PROB", "PROB_SCALE",
    "MoneyError", "to_decimal",
    "quantize_prob", "quantize_usd", "clamp_prob",
    "cents_to_prob", "prob_to_cents", "prob_to_centi", "centi_to_prob",
    "round_up_cent", "round_down_cent", "fmt_usd", "fmt_prob", "fmt_cents",
]

CENT:   Final[Decimal] = Decimal("0.01")
CENTI:  Final[Decimal] = Decimal("0.0001")   # centi-cent
DOLLAR: Final[Decimal] = Decimal("1")
ZERO:   Final[Decimal] = Decimal("0")
ONE:    Final[Decimal] = Decimal("1")

PROB_SCALE: Final[int] = 10_000

# A binary contract is never worth exactly 0 or 1 while it trades. Clamping at
# the tick keeps every division by a price total.
MIN_PROB: Final[Decimal] = Decimal("0.0001")
MAX_PROB: Final[Decimal] = Decimal("0.9999")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as an exact quantity."""


def to_decimal(value: object, *, field: str = "value") -> Decimal:
    """
    Coerce an inbound value to Decimal without adding new error.

    Exchange JSON gives us ints, decimal strings, and occasionally floats. A
    float is already lossy on arrival, so it is routed through `repr` (shortest
    round-trip form) rather than Decimal(float), which would expand the full
    binary tail and bake it into every downstream digit.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MoneyError(f"{field}: bool is not a quantity")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"{field}: cannot parse {value!r}") from exc
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise MoneyError(f"{field}: non-finite float {value!r}")
        return Decimal(repr(value))
    raise MoneyError(f"{field}: unsupported type {type(value).__name__}")


def quantize_prob(value: object, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantise a probability/price to the centi-cent grid."""
    return to_decimal(value, field="prob").quantize(CENTI, rounding=rounding)


def quantize_usd(value: object, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantise a dollar amount to the centi-cent grid.

    Sub-cent precision is retained deliberately: fees and per-contract edges
    are fractions of a cent, and truncating them to whole cents mid-calculation
    is how a scanner reports edge that does not survive execution.
    """
    return to_decimal(value, field="usd").quantize(CENTI, rounding=rounding)


def clamp_prob(value: object) -> Decimal:
    p = quantize_prob(value)
    if p < MIN_PROB:
        return MIN_PROB
    if p > MAX_PROB:
        return MAX_PROB
    return p


# ----------------------------------------------------------------------
# Venue <-> canonical conversions
# ----------------------------------------------------------------------

def cents_to_prob(cents: object) -> Decimal:
    """Kalshi integer cents (1..99) -> probability. Exact by construction."""
    c = to_decimal(cents, field="cents")
    return quantize_prob(c / 100)


def prob_to_cents(value: object, *, rounding: str = ROUND_HALF_UP) -> int:
    """
    Probability -> Kalshi integer cents.

    Kalshi will not accept a sub-cent limit price, so this is a real constraint
    on order placement, not a display concern. Callers crossing a spread should
    pass ROUND_CEILING when buying and ROUND_FLOOR when selling so the rounded
    price is never better than the one the edge was computed against.
    """
    p = clamp_prob(value)
    return int((p * 100).quantize(Decimal("1"), rounding=rounding))


def prob_to_centi(value: object) -> int:
    """Probability -> integer centi-cents, for exact storage and comparison."""
    return int(quantize_prob(value) * PROB_SCALE)


def centi_to_prob(centi: int | None) -> Decimal:
    if centi is None:
        return ZERO
    return (Decimal(int(centi)) / PROB_SCALE).quantize(CENTI)


def round_up_cent(value: object) -> Decimal:
    """Round a dollar amount up to the next whole cent (fee convention)."""
    return to_decimal(value, field="usd").quantize(CENT, rounding=ROUND_CEILING)


def round_down_cent(value: object) -> Decimal:
    return to_decimal(value, field="usd").quantize(CENT, rounding=ROUND_FLOOR)


# ----------------------------------------------------------------------
# Display boundary
# ----------------------------------------------------------------------

def fmt_usd(value: object, places: int = 2) -> str:
    q = Decimal(1).scaleb(-places)
    return f"${to_decimal(value).quantize(q, rounding=ROUND_HALF_UP):,f}"


def fmt_prob(value: object) -> str:
    return f"{quantize_prob(value):f}"


def fmt_cents(value: object) -> str:
    return f"{prob_to_cents(value)}c"
