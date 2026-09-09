"""
Venue fee models.

A cross-venue binary spread is typically 1-3 cents wide. Kalshi's trading fee
at a 50c price is 1.75c per contract. The fee is therefore not a correction to
the edge — at mid prices it is frequently *larger* than the edge, and a scanner
that reports gross spreads will hand you a list of trades that all lose money.
Fees are applied before a spread is ever called actionable.

Kalshi's published trading fee is

    fee = roundup_to_cent( coefficient * C * P * (1 - P) )

with coefficient 0.07 on most markets, C contracts and P the price in dollars.
The P(1-P) term peaks at 0.25, so the fee is worst at 50c and falls toward the
wings — which is why cross-venue edges are easiest to realise on lopsided
markets, and why the scanner reports fee-inclusive edge per contract rather
than a spread width.

IMPORTANT: fee schedules change, and both venues have run promotional and
maker-specific rates. Every coefficient here is configurable and the defaults
carry a `verified_on` date. Treat a stale date as a reason to re-check the live
schedule before sizing anything, not as a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from money import ZERO, clamp_prob, quantize_usd, round_up_cent, to_decimal

# Date the bundled defaults were last checked against each venue's public
# schedule. Bump this whenever you re-verify.
DEFAULTS_VERIFIED_ON: Final[str] = "unverified — check before live sizing"


@dataclass(frozen=True)
class KalshiFees:
    """
    Kalshi trading fees.

    `taker_coefficient` is the 0.07 in the published formula. `maker_fee_per_
    contract` covers the flat maker fee Kalshi applies on some markets; it is
    zero by default because it does not apply everywhere.
    """

    taker_coefficient: Decimal = Decimal("0.07")
    maker_fee_per_contract: Decimal = ZERO
    settlement_fee_per_contract: Decimal = ZERO

    def trade_fee(self, contracts: int, price: object, *, maker: bool = False) -> Decimal:
        """Fee in dollars for `contracts` at `price` (probability, 0-1)."""
        if contracts <= 0:
            return ZERO
        p = clamp_prob(price)
        if maker:
            return quantize_usd(self.maker_fee_per_contract * contracts)
        raw = self.taker_coefficient * Decimal(contracts) * p * (Decimal(1) - p)
        return round_up_cent(raw)

    def settlement_fee(self, contracts: int) -> Decimal:
        if contracts <= 0:
            return ZERO
        return quantize_usd(self.settlement_fee_per_contract * Decimal(contracts))

    def round_trip_to_settlement(
        self, contracts: int, price: object, *, maker: bool = False
    ) -> Decimal:
        """
        Total fee for entering and holding to settlement.

        Cross-venue arbitrage holds to resolution rather than trading out, so
        there is no exit fee — but there may be a settlement fee, and omitting
        it overstates edge on every pair.
        """
        return quantize_usd(
            self.trade_fee(contracts, price, maker=maker) + self.settlement_fee(contracts)
        )


@dataclass(frozen=True)
class PolymarketFees:
    """
    Polymarket costs.

    The CLOB has historically charged no trading fee, but 'no trading fee' is
    not 'no cost': orders settle on-chain, and any per-trade gas or relayer
    cost is a real subtraction from a 1-cent edge. `fixed_cost_per_trade` is
    where that goes, and it defaults to zero only because it depends on how you
    are transacting.
    """

    proportional_fee: Decimal = ZERO          # fraction of notional
    fixed_cost_per_trade: Decimal = ZERO      # gas / relayer, dollars per fill

    def trade_fee(self, shares: object, price: object) -> Decimal:
        qty = to_decimal(shares, field="shares")
        if qty <= ZERO:
            return ZERO
        p = clamp_prob(price)
        return quantize_usd(self.proportional_fee * qty * p + self.fixed_cost_per_trade)

    def round_trip_to_settlement(self, shares: object, price: object) -> Decimal:
        return self.trade_fee(shares, price)


DEFAULT_KALSHI_FEES: Final[KalshiFees] = KalshiFees()
DEFAULT_POLYMARKET_FEES: Final[PolymarketFees] = PolymarketFees()
