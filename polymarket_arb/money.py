"""
Fixed-point money, price and share primitives.

Every value that can end up in a balance, a fill price, or a PnL figure flows
through this module.  Floats are permitted only at the two lossy boundaries we
do not control — the exchange JSON payload on the way in, and the log/display
formatter on the way out.  Everything between them is `Decimal`.

Units
-----
    USDC    quantised to 1e-6   (USDC's native on-chain precision)
    price   quantised to 1e-4   (centi-cent; Polymarket ticks at 1e-2/1e-3,
                                 Kalshi quotes cents, so 1e-4 holds both
                                 without ever needing a re-quantise)
    shares  quantised to 1e-6

Persistence
-----------
Money never touches SQLite's REAL affinity — REAL is a C double, so a
round-trip through it silently re-introduces the error this module exists to
prevent, and `SUM(pnl)` over a day of trades accumulates it.  Values are stored
as INTEGER minor units (micro-USDC, centi-cents) which are exact under SQLite's
64-bit integer arithmetic, including under SUM() and MAX().

Rounding policy
---------------
Two rules, and the split between them matters:

    valuation  (`notional`)        ROUND_HALF_UP, deterministic
    sizing     (`shares_for_cost`) ROUND_DOWN, conservative

Valuing a share block is a *pure function of (shares, price)*: the same pair
must always produce the same USDC figure. It is tempting to round costs up and
proceeds down "to be safe", but that makes valuation asymmetric across a
position's life — closing a position at the exact price it opened at then
returns a small loss instead of zero, and the bot manufactures PnL out of
rounding. Conservatism belongs in sizing decisions (never buy past the budget)
and in explicitly modelled costs (slippage, fees), where it is visible and
auditable, never hidden inside a rounding mode.

Why shares, not notional, are the invariant
-------------------------------------------
`shares = cost / price` is the inexact operation in the position lifecycle.
Storing notional and re-deriving shares on every mark and close (as the previous
implementation did) re-runs that division repeatedly and lets the error compound
across the position's life.  The exchange fills in *shares*, so shares are what
we quantise once at entry and carry; cost and proceeds are then exact products.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Final

__all__ = [
    "USDC_QUANTUM", "PRICE_QUANTUM", "SHARE_QUANTUM",
    "MIN_PRICE", "MAX_PRICE", "ZERO",
    "to_decimal", "quantize_usdc", "quantize_price", "quantize_shares",
    "clamp_price", "shares_for_cost", "notional",
    "usdc_to_micro", "micro_to_usdc", "price_to_centi", "centi_to_price",
    "shares_to_micro", "micro_to_shares", "fmt_usdc", "fmt_price",
]

USDC_QUANTUM:  Final[Decimal] = Decimal("0.000001")
PRICE_QUANTUM: Final[Decimal] = Decimal("0.0001")
SHARE_QUANTUM: Final[Decimal] = Decimal("0.000001")

USDC_SCALE:  Final[int] = 1_000_000
PRICE_SCALE: Final[int] = 10_000
SHARE_SCALE: Final[int] = 1_000_000

# Binary contracts are worth strictly more than 0 and strictly less than 1.
# Clamping here is what makes division by price total.
MIN_PRICE: Final[Decimal] = Decimal("0.0001")
MAX_PRICE: Final[Decimal] = Decimal("0.9999")

ZERO: Final[Decimal] = Decimal("0")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as an exact quantity."""


def to_decimal(value: object, *, field: str = "value") -> Decimal:
    """
    Coerce an inbound value to Decimal without introducing new error.

    Decimal/int/str convert exactly.  A float is already lossy by the time we
    see it, so it is routed through `repr` (shortest round-trip form) rather
    than Decimal(float), which would expand the full binary expansion and bake
    the representation error into every downstream digit.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"{field}: cannot parse {value!r} as a number") from exc
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise MoneyError(f"{field}: non-finite float {value!r}")
        return Decimal(repr(value))
    raise MoneyError(f"{field}: unsupported type {type(value).__name__}")


def quantize_usdc(value: object, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    return to_decimal(value, field="usdc").quantize(USDC_QUANTUM, rounding=rounding)


def quantize_price(value: object, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    return to_decimal(value, field="price").quantize(PRICE_QUANTUM, rounding=rounding)


def quantize_shares(value: object, *, rounding: str = ROUND_DOWN) -> Decimal:
    return to_decimal(value, field="shares").quantize(SHARE_QUANTUM, rounding=rounding)


def clamp_price(value: object) -> Decimal:
    """Quantise to the price tick and clamp into (0, 1) exclusive."""
    price = quantize_price(value)
    if price < MIN_PRICE:
        return MIN_PRICE
    if price > MAX_PRICE:
        return MAX_PRICE
    return price


def shares_for_cost(cost_usdc: object, price: object) -> Decimal:
    """
    How many shares a given USDC budget buys at `price`.

    ROUND_DOWN: an order must never exceed the size the risk layer approved.
    This is the one place conservatism is applied, and it is applied once, at
    sizing time.
    """
    cost = quantize_usdc(cost_usdc)
    px = clamp_price(price)
    if cost <= ZERO:
        return ZERO
    return quantize_shares(cost / px, rounding=ROUND_DOWN)


def notional(shares: object, price: object) -> Decimal:
    """
    USDC value of `shares` at `price`.

    Deterministic and symmetric: entry cost, mark-to-market and exit proceeds
    all go through this one function, so revaluing an unchanged position at an
    unchanged price is exactly a no-op.
    """
    qty = quantize_shares(shares)
    px = clamp_price(price)
    if qty <= ZERO:
        return ZERO
    return quantize_usdc(qty * px, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# Persistence codecs — Decimal <-> exact INTEGER minor units
# ----------------------------------------------------------------------

def usdc_to_micro(value: object) -> int:
    return int(quantize_usdc(value) * USDC_SCALE)


def micro_to_usdc(micro: int | None) -> Decimal:
    if micro is None:
        return ZERO
    return (Decimal(int(micro)) / USDC_SCALE).quantize(USDC_QUANTUM)


def price_to_centi(value: object) -> int:
    return int(quantize_price(value) * PRICE_SCALE)


def centi_to_price(centi: int | None) -> Decimal:
    if centi is None:
        return ZERO
    return (Decimal(int(centi)) / PRICE_SCALE).quantize(PRICE_QUANTUM)


def shares_to_micro(value: object) -> int:
    return int(quantize_shares(value) * SHARE_SCALE)


def micro_to_shares(micro: int | None) -> Decimal:
    if micro is None:
        return ZERO
    return (Decimal(int(micro)) / SHARE_SCALE).quantize(SHARE_QUANTUM)


# ----------------------------------------------------------------------
# Display boundary — the only place a money value is allowed to lose precision
# ----------------------------------------------------------------------

def fmt_usdc(value: object, places: int = 2) -> str:
    quantum = Decimal(1).scaleb(-places)
    return f"{to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP):f}"


def fmt_price(value: object) -> str:
    return f"{quantize_price(value):f}"
