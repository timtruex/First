"""
Cross-venue spread detection.

The trade
---------
Buy YES on the cheap venue and NO on the other. If the two markets resolve
identically, exactly one leg pays $1 per contract at settlement, so:

    profit_per_contract = 1 - price_yes - price_no - fees

Edge exists whenever the two prices sum to less than a dollar by more than the
round-trip fees. Both directions are priced (Kalshi-YES/Poly-NO and
Poly-YES/Kalshi-NO) because which venue is cheap flips constantly.

Sizing walks both books together
--------------------------------
Quoting an edge off the top of book and then sizing into it is the standard way
a scanner reports profit that does not exist: the second contract fills at a
worse price than the first. This walks both ask ladders in lockstep and stops
at the first level pair where the marginal contract stops being profitable, so
the reported size is the size that actually clears at the reported edge.

Fills are all-or-nothing per leg. A partial fill on one side is not a smaller
arbitrage — it is an unhedged directional position in a market you had no view
on — so `cost_to_buy` returns None rather than a partial.

What this does NOT model
------------------------
  - Legging risk. The two venues are separate exchanges with separate latency;
    between filling leg one and leg two the second can move. This is the main
    live-execution risk and it is not a scanning concern.
  - Capital transfer time between venues.
  - Resolution divergence, which is handled upstream in pairing.py and is the
    difference between this being arbitrage and being a coin flip.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fees import DEFAULT_KALSHI_FEES, DEFAULT_POLYMARKET_FEES, KalshiFees, PolymarketFees
from money import ONE, ZERO, quantize_prob, quantize_usd
from orderbook import Book
from pairing import MarketPair

Direction = Literal["KALSHI_YES_POLY_NO", "POLY_YES_KALSHI_NO"]


@dataclass(frozen=True)
class Spread:
    """A priced, size-limited cross-venue opportunity."""

    pair_id: str
    direction: Direction
    contracts: int
    kalshi_price: Decimal          # VWAP actually paid on the Kalshi leg
    poly_price: Decimal            # VWAP actually paid on the Polymarket leg
    gross_edge_per_contract: Decimal
    fees_total: Decimal
    net_profit: Decimal
    capital_required: Decimal
    tradeable: bool
    block_reason: str | None
    days_to_resolution: Decimal | None = None
    ts: float = 0.0

    @property
    def roi(self) -> Decimal:
        if self.capital_required <= ZERO:
            return ZERO
        return (self.net_profit / self.capital_required).quantize(Decimal("0.000001"))

    @property
    def annualised_roi(self) -> Decimal | None:
        """
        Simple (not compounded) annualisation.

        Capital is locked until resolution, so a 1% edge over three days and a
        1% edge over nine months are entirely different trades and ranking on
        raw ROI would prefer the wrong one. Simple rather than compounded
        because you cannot assume the same opportunity recurs on redeployment.
        """
        if self.days_to_resolution is None or self.days_to_resolution <= ZERO:
            return None
        return (self.roi * (Decimal(365) / self.days_to_resolution)).quantize(Decimal("0.0001"))

    @property
    def net_edge_per_contract(self) -> Decimal:
        if self.contracts <= 0:
            return ZERO
        return quantize_usd(self.net_profit / self.contracts)


def _days_until(iso_ts: str | None) -> Decimal | None:
    if not iso_ts:
        return None
    try:
        when = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return None
    return (Decimal(delta) / Decimal(86400)).quantize(Decimal("0.0001"))


def _kalshi_marginal_rate(price: Decimal, fees: KalshiFees) -> Decimal:
    """Per-contract fee rate at `price`, before the total is rounded up."""
    return fees.taker_coefficient * price * (ONE - price)


def _walk_two_books(
    book_a: Book,
    book_b: Book,
    *,
    kalshi_is_a: bool,
    kalshi_fees: KalshiFees,
    poly_fees: PolymarketFees,
) -> tuple[int, Decimal, Decimal] | None:
    """
    Walk both ask ladders together while the marginal contract is profitable.

    Returns (contracts, cost_a, cost_b) or None if no profitable size exists.
    """
    if not book_a.asks or not book_b.asks:
        return None

    i = j = 0
    rem_a = book_a.asks[0].size
    rem_b = book_b.asks[0].size
    qty = ZERO
    cost_a = ZERO
    cost_b = ZERO

    while i < len(book_a.asks) and j < len(book_b.asks):
        p_a = book_a.asks[i].price
        p_b = book_b.asks[j].price

        k_price = p_a if kalshi_is_a else p_b
        marginal_fee = (
            _kalshi_marginal_rate(k_price, kalshi_fees)
            + kalshi_fees.settlement_fee_per_contract
            + poly_fees.proportional_fee * (p_b if kalshi_is_a else p_a)
        )
        if p_a + p_b + marginal_fee >= ONE:
            break

        take = min(rem_a, rem_b)
        if take <= ZERO:
            break

        qty += take
        cost_a += take * p_a
        cost_b += take * p_b
        rem_a -= take
        rem_b -= take

        if rem_a <= ZERO:
            i += 1
            if i < len(book_a.asks):
                rem_a = book_a.asks[i].size
        if rem_b <= ZERO:
            j += 1
            if j < len(book_b.asks):
                rem_b = book_b.asks[j].size

    # Kalshi settles whole contracts, so the executable size is the floor. The
    # floor is taken once, at the end, rather than per level — flooring while
    # walking would discard fractional depth that later levels can complete.
    contracts = int(qty)
    if contracts <= 0:
        return None

    # Re-price the truncated quantity against the books so cost matches the
    # size we would actually send, not the fractional walk.
    filled_a = book_a.cost_to_buy(Decimal(contracts))
    filled_b = book_b.cost_to_buy(Decimal(contracts))
    if filled_a is None or filled_b is None:
        return None
    return contracts, filled_a[0], filled_b[0]


def price_direction(
    pair: MarketPair,
    *,
    direction: Direction,
    kalshi_book: Book,
    poly_book: Book,
    kalshi_fees: KalshiFees = DEFAULT_KALSHI_FEES,
    poly_fees: PolymarketFees = DEFAULT_POLYMARKET_FEES,
) -> Spread | None:
    """Price one direction of one pair. Returns None if there is no edge."""
    kalshi_is_a = direction == "KALSHI_YES_POLY_NO"
    book_a, book_b = (kalshi_book, poly_book) if kalshi_is_a else (poly_book, kalshi_book)

    walked = _walk_two_books(
        book_a, book_b,
        kalshi_is_a=kalshi_is_a, kalshi_fees=kalshi_fees, poly_fees=poly_fees,
    )
    if walked is None:
        return None
    contracts, cost_a, cost_b = walked

    k_cost, p_cost = (cost_a, cost_b) if kalshi_is_a else (cost_b, cost_a)
    k_vwap = quantize_prob(k_cost / contracts)
    p_vwap = quantize_prob(p_cost / contracts)

    gross_payout = Decimal(contracts) * ONE
    gross_cost = quantize_usd(k_cost + p_cost)
    gross_edge = quantize_usd((gross_payout - gross_cost) / contracts)

    total_fees = quantize_usd(
        kalshi_fees.round_trip_to_settlement(contracts, k_vwap)
        + poly_fees.round_trip_to_settlement(Decimal(contracts), p_vwap)
    )
    capital = quantize_usd(gross_cost + total_fees)
    net_profit = quantize_usd(gross_payout - capital)

    if net_profit <= ZERO:
        return None

    return Spread(
        pair_id=pair.pair_id,
        direction=direction,
        contracts=contracts,
        kalshi_price=k_vwap,
        poly_price=p_vwap,
        gross_edge_per_contract=gross_edge,
        fees_total=total_fees,
        net_profit=net_profit,
        capital_required=capital,
        tradeable=pair.is_tradeable,
        block_reason=pair.block_reason(),
        days_to_resolution=_days_until(pair.kalshi.close_time),
        ts=time.time(),
    )


def scan_pair(
    pair: MarketPair,
    *,
    kalshi_yes: Book,
    kalshi_no: Book,
    poly_yes: Book,
    poly_no: Book,
    kalshi_fees: KalshiFees = DEFAULT_KALSHI_FEES,
    poly_fees: PolymarketFees = DEFAULT_POLYMARKET_FEES,
) -> list[Spread]:
    """Price both directions of a pair, best net profit first."""
    found = [
        price_direction(
            pair, direction="KALSHI_YES_POLY_NO",
            kalshi_book=kalshi_yes, poly_book=poly_no,
            kalshi_fees=kalshi_fees, poly_fees=poly_fees,
        ),
        price_direction(
            pair, direction="POLY_YES_KALSHI_NO",
            kalshi_book=kalshi_no, poly_book=poly_yes,
            kalshi_fees=kalshi_fees, poly_fees=poly_fees,
        ),
    ]
    spreads = [s for s in found if s is not None]
    spreads.sort(key=lambda s: s.net_profit, reverse=True)
    return spreads
