"""
Venue-neutral order book.

Both venues are normalised into the same shape so the spread math never has to
branch on venue. Kalshi quotes a YES book and a NO book in integer cents;
Polymarket quotes one book per outcome token in decimal prices. Both reduce to
"levels of (probability, size) sorted best-first".

Sizes are kept as Decimal because Polymarket shares are fractional; Kalshi
contract counts are whole numbers and are floored back to integers at the point
an order would actually be placed, never before, so depth is not silently lost
while walking the book.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from money import ZERO, clamp_prob, quantize_prob, to_decimal


@dataclass(frozen=True)
class Level:
    price: Decimal      # probability, 0-1
    size: Decimal       # contracts or shares available at this price


@dataclass(frozen=True)
class Book:
    """One side of one outcome. `asks` are what you pay to buy."""

    venue: str
    market_id: str
    outcome: str                 # "YES" | "NO"
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    ts: float = 0.0

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def is_empty(self) -> bool:
        return not self.asks and not self.bids

    def depth_at_or_better(self, limit_price: Decimal) -> Decimal:
        """Total size buyable at or below `limit_price`."""
        return sum((lvl.size for lvl in self.asks if lvl.price <= limit_price), ZERO)

    def cost_to_buy(self, quantity: Decimal) -> tuple[Decimal, Decimal] | None:
        """
        Walk the ask side for `quantity`.

        Returns (total_cost, volume_weighted_price), or None if the book cannot
        fill the whole quantity. Returning None rather than a partial fill is
        deliberate: a cross-venue arb that fills one leg and not the other is
        not a smaller arb, it is an unhedged directional position.
        """
        if quantity <= ZERO:
            return None
        remaining = quantity
        cost = ZERO
        for lvl in self.asks:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            remaining -= take
            if remaining <= ZERO:
                break
        if remaining > ZERO:
            return None
        vwap = quantize_prob(cost / quantity)
        return cost, vwap


def _levels(raw: Sequence, *, ascending: bool) -> tuple[Level, ...]:
    out = [
        Level(price=clamp_prob(p), size=to_decimal(s, field="size"))
        for p, s in raw
        if to_decimal(s, field="size") > ZERO
    ]
    out.sort(key=lambda l: l.price, reverse=not ascending)
    return tuple(out)


def book_from_kalshi(
    market_id: str, outcome: str, *, yes_levels: Sequence, no_levels: Sequence, ts: float = 0.0
) -> Book:
    """
    Build one outcome's book from Kalshi's two-sided quote.

    Kalshi publishes resting *bids* on both the YES and NO books in cents. A
    bid of Nc on the NO book is economically an offer to sell YES at (100-N)c,
    which is where the YES ask comes from — Kalshi has no separate ask book.
    Getting this inversion wrong silently prices every spread against the wrong
    side, so it is done here, once.
    """
    def to_prob_levels(levels: Sequence) -> list[tuple[Decimal, Decimal]]:
        return [(Decimal(int(p)) / 100, to_decimal(s, field="size")) for p, s in levels]

    yes_bids = to_prob_levels(yes_levels)
    no_bids = to_prob_levels(no_levels)

    if outcome == "YES":
        bids = yes_bids
        asks = [(Decimal(1) - p, s) for p, s in no_bids]
    else:
        bids = no_bids
        asks = [(Decimal(1) - p, s) for p, s in yes_bids]

    return Book(
        venue="kalshi", market_id=market_id, outcome=outcome,
        bids=_levels(bids, ascending=False),
        asks=_levels(asks, ascending=True),
        ts=ts,
    )


def book_from_polymarket(
    token_id: str, outcome: str, *, bids: Sequence, asks: Sequence, ts: float = 0.0
) -> Book:
    """Build a book from Polymarket CLOB levels (already decimal prices)."""
    return Book(
        venue="polymarket", market_id=token_id, outcome=outcome,
        bids=_levels([(p, s) for p, s in bids], ascending=False),
        asks=_levels([(p, s) for p, s in asks], ascending=True),
        ts=ts,
    )
