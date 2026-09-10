"""
Core arbitrage detection and position-sizing engine.

Signal detection
----------------
For each (asset, window, direction) contract we compare:
  - poly_price  : Polymarket mid-price  (implied probability, 0–1)
  - cex_implied : CEX-derived probability from BinanceFeed

A *lag* exists when |cex_implied - poly_price| > LAG_THRESHOLD_PCT / 100.
An *edge* is the raw difference less an estimated fee cost.

Kelly sizing (half-Kelly)
-------------------------
    f* = (p * b - q) / b       where b = (1/poly_price) - 1  (decimal odds)
    position = 0.5 * f* * equity

All sizing is capped at MAX_POSITION_FRACTION * equity.

Confidence model
----------------
A simple heuristic combines:
  - magnitude of the lag
  - how fresh the CEX data is
  - spread width on Polymarket (tight spread → more liquid → higher confidence)
  - directional consistency between the 5-min and 15-min signals

The score is normalised to [0, 100].
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from config import (
    CONTRACT_TOKEN_IDS,
    KELLY_FRACTION,
    LAG_THRESHOLD_PCT,
    MAX_DAILY_DRAWDOWN,
    MAX_POSITION_FRACTION,
    MIN_CONFIDENCE,
    MIN_EDGE_PCT,
    get_window_configs,
)
from money import ZERO, clamp_price, quantize_usdc, to_decimal

if TYPE_CHECKING:
    from binance_feed import BinanceFeed
    from polymarket_feed import PolymarketFeed

logger = logging.getLogger(__name__)

# Estimated round-trip fee on Polymarket (maker + taker, fraction)
_POLY_FEE_FRACTION = 0.02


@dataclass
class ArbitrageSignal:
    ts:               float
    contract_key:     str          # e.g. "BTC_5M_UP"
    token_id:         str
    asset:            str          # "BTC" | "ETH"
    window_min:       int          # 5 | 15
    direction:        str          # "UP" | "DOWN"
    poly_price:       Decimal      # Polymarket mid  (0–1), price-tick exact
    cex_implied_prob: float        # CEX model output (0–1)
    lag_pct:          float        # abs difference * 100
    edge_pct:         float        # lag minus fee estimate
    confidence:       float        # 0–100
    recommended_side: str          # "BUY" | "SELL" on Polymarket
    kelly_size_usdc:  Decimal      # suggested position size, USDC
    is_actionable:    bool         # passes all filters
    skip_reason:      str | None   # if not actionable, why


class ArbitrageEngine:
    """
    Scans all configured contracts for latency-arbitrage opportunities
    and emits ArbitrageSignal objects.
    """

    def __init__(
        self,
        binance: "BinanceFeed",
        polymarket: "PolymarketFeed",
        portfolio_equity_fn: "callable[[], Decimal]",
        open_position_value_fn: "callable[[], Decimal]",
    ) -> None:
        self._binance   = binance
        self._poly      = polymarket
        self._equity_fn = portfolio_equity_fn
        self._open_val_fn = open_position_value_fn
        self._configs   = get_window_configs()

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    def scan(self) -> list[ArbitrageSignal]:
        """
        Run one scan pass over all contracts.
        Returns a list of signals (actionable or not).
        """
        signals: list[ArbitrageSignal] = []
        equity = self._equity_fn()

        for cfg in self._configs:
            asset_sym = f"{cfg.asset.lower()}usdt"
            for direction in ("UP", "DOWN"):
                contract_key = f"{cfg.asset}_{cfg.window_min}M_{direction}"
                token_id_key = f"{cfg.asset}_{cfg.window_min}M_{direction}"
                token_id = CONTRACT_TOKEN_IDS.get(token_id_key, "")
                if not token_id:
                    continue  # not configured

                sig = self._evaluate(
                    contract_key=contract_key,
                    token_id=token_id,
                    asset_sym=asset_sym,
                    asset=cfg.asset,
                    window_min=cfg.window_min,
                    direction=direction,
                    equity=equity,
                )
                if sig:
                    signals.append(sig)

        return signals

    # ------------------------------------------------------------------
    # Per-contract evaluation
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        contract_key: str,
        token_id: str,
        asset_sym: str,
        asset: str,
        window_min: int,
        direction: str,
        equity: Decimal,
    ) -> ArbitrageSignal | None:

        snap = self._poly.get_snapshot(token_id)
        if snap is None or not snap.is_valid:
            return None
        if snap.is_stale():
            return None

        # The feed hands us a float; clamp_price is the single conversion point
        # to the price tick. poly_f is used only for dimensionless statistics.
        poly_price = clamp_price(snap.mid)
        poly_f = float(poly_price)

        # CEX implied probability
        cex_up_prob = self._binance.get_implied_prob(asset_sym, window_min)
        if cex_up_prob is None:
            return None

        cex_implied = cex_up_prob if direction == "UP" else (1.0 - cex_up_prob)

        lag_pct  = abs(cex_implied - poly_f) * 100.0
        fee_cost = _POLY_FEE_FRACTION * 100.0          # convert to pct
        edge_pct = lag_pct - fee_cost

        confidence = self._compute_confidence(
            lag_pct=lag_pct,
            cex_up_prob=cex_up_prob,
            asset_sym=asset_sym,
            window_min=window_min,
            direction=direction,
            spread=snap.spread,
        )

        # Determine recommended side
        if cex_implied > poly_f:
            # CEX says price should be higher → Polymarket is underpriced → BUY
            recommended_side = "BUY"
        else:
            # Polymarket is overpriced relative to CEX → SELL
            recommended_side = "SELL"

        # Kelly sizing
        kelly_size = self._kelly_size(poly_price, cex_implied, equity, recommended_side)

        # Actionability filters
        is_actionable, skip_reason = self._check_filters(
            lag_pct=lag_pct,
            edge_pct=edge_pct,
            confidence=confidence,
            kelly_size=kelly_size,
            equity=equity,
        )

        return ArbitrageSignal(
            ts=time.time(),
            contract_key=contract_key,
            token_id=token_id,
            asset=asset,
            window_min=window_min,
            direction=direction,
            poly_price=poly_price,
            cex_implied_prob=cex_implied,
            lag_pct=lag_pct,
            edge_pct=edge_pct,
            confidence=confidence,
            recommended_side=recommended_side,
            kelly_size_usdc=kelly_size,
            is_actionable=is_actionable,
            skip_reason=skip_reason,
        )

    # ------------------------------------------------------------------
    # Kelly Criterion
    # ------------------------------------------------------------------

    def _kelly_size(
        self,
        poly_price: Decimal,
        cex_implied: float,
        equity: Decimal,
        side: str,
    ) -> Decimal:
        """
        Half-Kelly sizing capped at MAX_POSITION_FRACTION * equity.

        For a binary contract:
          b = (1 / poly_price) - 1   (decimal odds for a BUY)
          p = cex_implied            (our estimated win probability)
          q = 1 - p

        Kelly fraction: f* = (p*b - q) / b = p - q/b
        Position size  = KELLY_FRACTION * f* * equity

        The Kelly fraction itself is a dimensionless statistic derived from a
        noisy probability estimate, so float is the honest type for it — an
        exact `f*` from an estimated `p` is false precision.  The fraction
        becomes money only on the final multiply by equity, which is where the
        result must be exact and is therefore done in Decimal and rounded down
        to the cent.  Rounding down means quantisation can only ever shrink a
        position, never push it past a risk cap.
        """
        px = float(poly_price)

        if side == "BUY":
            # We buy at px, win (1 - px) on success
            b = (1.0 - px) / px
            p = cex_implied
        else:
            # We sell (buy the DOWN contract) at (1 - px)
            effective_price = 1.0 - px
            effective_price = max(effective_price, 1e-4)
            b = (1.0 - effective_price) / effective_price
            p = 1.0 - cex_implied

        q = 1.0 - p

        if b <= 0 or p <= 0:
            return ZERO

        kelly_f = (p * b - q) / b
        kelly_f = max(kelly_f, 0.0)          # never negative

        # Half-Kelly
        half_kelly_f = KELLY_FRACTION * kelly_f

        # Cap at max position fraction
        capped_f = min(half_kelly_f, MAX_POSITION_FRACTION)

        # Crossing into money: exact from here down.
        max_fraction = to_decimal(MAX_POSITION_FRACTION, field="max_position_fraction")
        size = quantize_usdc(
            to_decimal(capped_f, field="kelly_fraction") * equity,
            rounding=ROUND_DOWN,
        )

        # Also cap by remaining budget not already in open positions
        open_val = self._open_val_fn()
        remaining_budget = quantize_usdc(
            equity * max_fraction - open_val, rounding=ROUND_DOWN
        )
        if remaining_budget < ZERO:
            remaining_budget = ZERO
        size = min(size, remaining_budget)

        return quantize_usdc(size, rounding=ROUND_DOWN)

    # ------------------------------------------------------------------
    # Confidence model
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        lag_pct: float,
        cex_up_prob: float,
        asset_sym: str,
        window_min: int,
        direction: str,
        spread: float,
    ) -> float:
        """
        Heuristic confidence score (0–100).

        Components:
        1. Lag magnitude   – larger lag → more confident  (0–40 pts)
        2. Spread penalty  – tight spread → liquid market (0–20 pts)
        3. Direction consistency between 5m and 15m windows (0–20 pts)
        4. CEX data freshness                               (0–20 pts)
        """
        # 1. Lag score: full credit at 10 pct lag, scale linearly
        lag_score = min(lag_pct / 10.0, 1.0) * 40.0

        # 2. Spread score: 0 spread → 20 pts, 0.10 spread → 0 pts
        spread_score = max(0.0, 1.0 - spread / 0.10) * 20.0

        # 3. Cross-window consistency
        other_window = 15 if window_min == 5 else 5
        other_prob = self._binance.get_implied_prob(asset_sym, other_window)
        consistency_score = 0.0
        if other_prob is not None:
            cex_other = other_prob if direction == "UP" else (1.0 - other_prob)
            cex_this  = cex_up_prob if direction == "UP" else (1.0 - cex_up_prob)
            if (cex_this > 0.5) == (cex_other > 0.5):
                consistency_score = 20.0

        # 4. Data freshness – penalise if CEX state is old
        binance_state = self._binance.get_state(asset_sym)
        freshness_score = 0.0
        if binance_state and not binance_state.is_stale(max_age_sec=5.0):
            freshness_score = 20.0
        elif binance_state and not binance_state.is_stale(max_age_sec=10.0):
            freshness_score = 10.0

        total = lag_score + spread_score + consistency_score + freshness_score
        return round(min(total, 100.0), 2)

    # ------------------------------------------------------------------
    # Filter checks
    # ------------------------------------------------------------------

    def _check_filters(
        self,
        lag_pct: float,
        edge_pct: float,
        confidence: float,
        kelly_size: Decimal,
        equity: Decimal,
    ) -> tuple[bool, str | None]:
        if lag_pct < LAG_THRESHOLD_PCT:
            return False, f"lag {lag_pct:.2f}% < threshold {LAG_THRESHOLD_PCT}%"
        if edge_pct < MIN_EDGE_PCT:
            return False, f"edge {edge_pct:.2f}% < min {MIN_EDGE_PCT}%"
        if confidence < MIN_CONFIDENCE:
            return False, f"confidence {confidence:.1f}% < min {MIN_CONFIDENCE}%"
        if kelly_size <= ZERO:
            return False, "kelly size is zero"
        # Guard the denominator without inflating a genuinely tiny book: an
        # equity at or below zero means the exposure fraction is unbounded, so
        # the cap must reject rather than divide.
        if equity <= ZERO:
            return False, "equity is zero or negative"
        open_fraction = float(self._open_val_fn() / equity)
        if open_fraction >= MAX_POSITION_FRACTION:
            return False, (
                f"open position fraction {open_fraction:.1%} >= "
                f"max {MAX_POSITION_FRACTION:.1%}"
            )
        return True, None


def compute_drawdown(current_equity: Decimal, peak_equity: Decimal) -> float:
    """
    Return current drawdown as a positive fraction (0–1).

    Inputs are exact; the ratio is returned as a float because it is compared
    against a float threshold and never re-enters the ledger.
    """
    if peak_equity <= ZERO:
        return 0.0
    return max(0.0, float((peak_equity - current_equity) / peak_equity))


def is_kill_switch_triggered(
    current_equity: Decimal,
    peak_equity: Decimal,
) -> tuple[bool, float]:
    dd = compute_drawdown(current_equity, peak_equity)
    return dd >= MAX_DAILY_DRAWDOWN, dd
