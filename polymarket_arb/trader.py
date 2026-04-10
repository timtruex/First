"""
Trade execution layer.

Supports two modes controlled by config.is_live_trading():

  Paper mode  – simulates fills at the current mid-price, records everything
                to the database, updates an in-memory portfolio.
  Live  mode  – calls the Polymarket CLOB via py-clob-client to place real
                limit orders at the mid or a configurable offset.

Safety requirements for live trading
-------------------------------------
  PAPER_TRADING=false   AND
  LIVE_CONFIRM_1=true   AND
  LIVE_CONFIRM_2=true   AND
  LIVE_CONFIRM_3=true

All three env vars must be present.  A missing or misspelled flag keeps
the bot in paper mode.  This is enforced in config.is_live_trading().
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from config import (
    POLY_PRIVATE_KEY,
    POLY_API_KEY,
    POLY_API_SECRET,
    POLY_API_PASSPHRASE,
    CHAIN_ID,
    CLOB_HOST,
    STARTING_PORTFOLIO_USDC,
    is_live_trading,
)
from database import Database

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType, BUY, SELL
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    ClobClient       = None  # type: ignore[assignment,misc]
    ApiCreds         = None  # type: ignore[assignment,misc]
    MarketOrderArgs  = None  # type: ignore[assignment,misc]
    OrderType        = None  # type: ignore[assignment,misc]
    BUY = SELL       = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass
class Position:
    trade_id:     int
    contract_key: str
    token_id:     str
    side:         str
    size_usdc:    float
    entry_price:  float
    opened_at:    float = field(default_factory=time.time)
    order_id:     str | None = None

    @property
    def current_value(self) -> float:
        """Approximate mark-to-market — updated by Trader.mark_positions()."""
        return self._mark  # type: ignore[attr-defined]

    def set_mark(self, mark_price: float) -> None:
        # Unrealised PnL: shares * (mark - entry) / entry * cost
        pnl_frac = (mark_price - self.entry_price) if self.side == "BUY" else (self.entry_price - mark_price)
        # Cost basis is size_usdc (= num_shares * entry_price approx)
        shares = self.size_usdc / self.entry_price
        self._mark = shares * mark_price  # type: ignore[attr-defined]


@dataclass
class PortfolioState:
    cash_usdc:    Decimal
    positions:    dict[int, Position] = field(default_factory=dict)

    @property
    def open_position_value(self) -> float:
        return sum(p.size_usdc for p in self.positions.values())

    @property
    def equity(self) -> float:
        return float(self.cash_usdc) + self.open_position_value

    def add_position(self, pos: Position) -> None:
        self.cash_usdc -= Decimal(str(pos.size_usdc))
        self.positions[pos.trade_id] = pos

    def close_position(self, trade_id: int, pnl_usdc: float) -> None:
        pos = self.positions.pop(trade_id, None)
        if pos:
            self.cash_usdc += Decimal(str(pos.size_usdc)) + Decimal(str(pnl_usdc))


class Trader:
    """
    Handles order lifecycle for both paper and live modes.
    """

    def __init__(self, db: Database) -> None:
        self._db    = db
        self._portfolio = PortfolioState(cash_usdc=STARTING_PORTFOLIO_USDC)
        self._live  = is_live_trading()
        self._client: ClobClient | None = self._build_client() if self._live else None

        mode_str = "LIVE" if self._live else "PAPER"
        logger.info("Trader initialised in %s mode. Starting equity=%.2f USDC",
                    mode_str, self._portfolio.equity)

        if self._live:
            logger.warning(
                "*** LIVE TRADING ACTIVE – real funds at risk ***"
            )

    # ------------------------------------------------------------------
    # Portfolio accessors used by the rest of the bot
    # ------------------------------------------------------------------

    def equity(self) -> float:
        return self._portfolio.equity

    def open_position_value(self) -> float:
        return self._portfolio.open_position_value

    def get_open_positions(self) -> list[Position]:
        return list(self._portfolio.positions.values())

    # ------------------------------------------------------------------
    # Order entry
    # ------------------------------------------------------------------

    async def enter(
        self,
        *,
        contract_key: str,
        token_id: str,
        side: str,          # "BUY" | "SELL"
        size_usdc: float,
        poly_price: float,
        cex_implied_prob: float,
        edge_pct: float,
        confidence: float,
        kelly_fraction: float,
    ) -> Position | None:
        """
        Open a position.  Returns a Position object on success, None on failure.
        """
        if size_usdc <= 0:
            logger.warning("Skipping %s %s – size %.2f ≤ 0", side, contract_key, size_usdc)
            return None

        if self._live:
            return await self._enter_live(
                contract_key=contract_key, token_id=token_id, side=side,
                size_usdc=size_usdc, poly_price=poly_price,
                cex_implied_prob=cex_implied_prob, edge_pct=edge_pct,
                confidence=confidence, kelly_fraction=kelly_fraction,
            )
        return await self._enter_paper(
            contract_key=contract_key, token_id=token_id, side=side,
            size_usdc=size_usdc, poly_price=poly_price,
            cex_implied_prob=cex_implied_prob, edge_pct=edge_pct,
            confidence=confidence, kelly_fraction=kelly_fraction,
        )

    async def _enter_paper(self, **kwargs: Any) -> Position | None:
        poly_price   = kwargs["poly_price"]
        side         = kwargs["side"]
        size_usdc    = kwargs["size_usdc"]
        contract_key = kwargs["contract_key"]
        token_id     = kwargs["token_id"]

        # Simulate a fill at mid-price with a small slippage penalty
        simulated_fill = poly_price * (1.001 if side == "BUY" else 0.999)
        simulated_fill = max(0.001, min(0.999, simulated_fill))

        trade_id = self._db.insert_trade(
            mode="paper",
            contract_key=contract_key,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            entry_price=simulated_fill,
            cex_implied_prob=kwargs["cex_implied_prob"],
            edge_pct=kwargs["edge_pct"],
            confidence=kwargs["confidence"],
            kelly_fraction=kwargs["kelly_fraction"],
            order_id=None,
        )

        pos = Position(
            trade_id=trade_id,
            contract_key=contract_key,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            entry_price=simulated_fill,
        )
        pos.set_mark(simulated_fill)
        self._portfolio.add_position(pos)

        logger.info(
            "[PAPER] Entered %s %s  size=%.2f USDC  price=%.4f",
            side, contract_key, size_usdc, simulated_fill,
        )
        return pos

    async def _enter_live(self, **kwargs: Any) -> Position | None:
        if not _CLOB_AVAILABLE or self._client is None:
            logger.error("Live trading requested but ClobClient not available.")
            return None

        poly_price   = kwargs["poly_price"]
        side         = kwargs["side"]
        size_usdc    = kwargs["size_usdc"]
        contract_key = kwargs["contract_key"]
        token_id     = kwargs["token_id"]

        clob_side = BUY if side == "BUY" else SELL
        # Size in tokens = USDC / price
        num_tokens = size_usdc / poly_price

        try:
            loop = asyncio.get_running_loop()
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=num_tokens,
            )
            response = await loop.run_in_executor(
                None,
                lambda: self._client.create_market_order(order_args),  # type: ignore[union-attr]
            )
            order_id   = str(response.get("orderID", "unknown"))
            fill_price = float(response.get("price", poly_price))
        except Exception as exc:
            logger.error("Live order failed for %s: %s", contract_key, exc)
            return None

        trade_id = self._db.insert_trade(
            mode="live",
            contract_key=contract_key,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            entry_price=fill_price,
            cex_implied_prob=kwargs["cex_implied_prob"],
            edge_pct=kwargs["edge_pct"],
            confidence=kwargs["confidence"],
            kelly_fraction=kwargs["kelly_fraction"],
            order_id=order_id,
        )

        pos = Position(
            trade_id=trade_id,
            contract_key=contract_key,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            entry_price=fill_price,
            order_id=order_id,
        )
        pos.set_mark(fill_price)
        self._portfolio.add_position(pos)

        logger.info(
            "[LIVE] Entered %s %s  size=%.2f USDC  price=%.4f  order_id=%s",
            side, contract_key, size_usdc, fill_price, order_id,
        )
        return pos

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    async def close_position(
        self,
        trade_id: int,
        exit_price: float,
    ) -> float:
        """
        Close an open position at exit_price.  Returns realised PnL in USDC.
        """
        pos = self._portfolio.positions.get(trade_id)
        if pos is None:
            logger.warning("close_position: trade_id %d not found.", trade_id)
            return 0.0

        shares = pos.size_usdc / pos.entry_price
        if pos.side == "BUY":
            pnl = shares * (exit_price - pos.entry_price)
        else:
            pnl = shares * (pos.entry_price - exit_price)

        self._db.close_trade(trade_id, exit_price, pnl)
        self._portfolio.close_position(trade_id, pnl)
        logger.info(
            "Closed trade #%d %s %s  exit=%.4f  PnL=%.4f USDC",
            trade_id, pos.side, pos.contract_key, exit_price, pnl,
        )
        return pnl

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def mark_positions(self, price_fn: "callable[[str], float | None]") -> None:
        """
        Update mark price for open positions using the supplied lookup function.
        price_fn receives a token_id and returns the current mid price or None.
        """
        for pos in self._portfolio.positions.values():
            mark = price_fn(pos.token_id)
            if mark is not None:
                pos.set_mark(mark)

    # ------------------------------------------------------------------
    # CLOB client setup
    # ------------------------------------------------------------------

    def _build_client(self) -> ClobClient | None:
        if not _CLOB_AVAILABLE:
            return None
        try:
            creds = ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_API_SECRET,
                api_passphrase=POLY_API_PASSPHRASE,
            )
            return ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                creds=creds,
            )
        except Exception as exc:
            logger.error("Could not build live ClobClient: %s", exc)
            return None
