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

Numeric policy
--------------
Cash, share counts, fill prices and PnL are `Decimal` end to end (see
money.py).  A position carries the *share count* as its invariant rather than
its notional, because `shares = notional / price` is the one inexact step in
the lifecycle and re-deriving it on every mark and close compounds that error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

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
from money import (
    ZERO,
    clamp_price,
    fmt_price,
    fmt_usdc,
    notional,
    quantize_shares,
    quantize_usdc,
    shares_for_cost,
    to_decimal,
)

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

# Paper-mode slippage: cross the touch by one tick-ish penalty so simulated
# fills are never better than a live fill would plausibly be.
_PAPER_SLIPPAGE = Decimal("0.001")


@dataclass
class Position:
    trade_id:     int
    contract_key: str
    token_id:     str
    side:         str            # "BUY" | "SELL"
    shares:       Decimal        # invariant: what the exchange actually filled
    entry_price:  Decimal
    cost_usdc:    Decimal        # capital committed at entry
    opened_at:    float = field(default_factory=time.time)
    order_id:     str | None = None
    mark_price:   Decimal | None = None

    def set_mark(self, mark_price: object) -> None:
        self.mark_price = clamp_price(mark_price)

    @property
    def effective_mark(self) -> Decimal:
        """Last known mark, falling back to entry before the first mark tick."""
        return self.mark_price if self.mark_price is not None else self.entry_price

    @property
    def current_value(self) -> Decimal:
        """
        Mark-to-market value of the committed capital.

        A BUY is worth what the shares would fetch now.  A SELL is carried at
        the existing short semantics — the position gains as the price falls —
        so its value is cost plus unrealised PnL.
        """
        if self.side == "BUY":
            return notional(self.shares, self.effective_mark)
        return quantize_usdc(self.cost_usdc + self.unrealised_pnl)

    @property
    def unrealised_pnl(self) -> Decimal:
        return self.pnl_at(self.effective_mark)

    def pnl_at(self, exit_price: object) -> Decimal:
        """
        Realised/unrealised PnL if the position were closed at `exit_price`.

        Both legs value the same share count through the same `notional`
        function, so closing at the entry price returns exactly zero rather
        than a dust residue that would slowly bias the daily PnL series.
        """
        px = clamp_price(exit_price)
        if self.side == "BUY":
            return quantize_usdc(notional(self.shares, px) - self.cost_usdc)
        return quantize_usdc(
            notional(self.shares, self.entry_price) - notional(self.shares, px)
        )


@dataclass
class PortfolioState:
    cash_usdc:    Decimal
    positions:    dict[int, Position] = field(default_factory=dict)

    @property
    def open_position_value(self) -> Decimal:
        return quantize_usdc(
            sum((p.current_value for p in self.positions.values()), ZERO)
        )

    @property
    def committed_capital(self) -> Decimal:
        """Cost basis of open positions, ignoring marks — used for budgeting."""
        return quantize_usdc(
            sum((p.cost_usdc for p in self.positions.values()), ZERO)
        )

    @property
    def equity(self) -> Decimal:
        return quantize_usdc(self.cash_usdc + self.open_position_value)

    def add_position(self, pos: Position) -> None:
        self.cash_usdc = quantize_usdc(self.cash_usdc - pos.cost_usdc)
        self.positions[pos.trade_id] = pos

    def close_position(self, trade_id: int, pnl_usdc: Decimal) -> None:
        pos = self.positions.pop(trade_id, None)
        if pos is not None:
            self.cash_usdc = quantize_usdc(self.cash_usdc + pos.cost_usdc + pnl_usdc)


class Trader:
    """
    Handles order lifecycle for both paper and live modes.
    """

    def __init__(self, db: Database) -> None:
        self._db    = db
        self._portfolio = PortfolioState(cash_usdc=quantize_usdc(STARTING_PORTFOLIO_USDC))
        self._live  = is_live_trading()
        self._client: ClobClient | None = self._build_client() if self._live else None

        mode_str = "LIVE" if self._live else "PAPER"
        logger.info("Trader initialised in %s mode. Starting equity=%s USDC",
                    mode_str, fmt_usdc(self._portfolio.equity))

        if self._live:
            logger.warning(
                "*** LIVE TRADING ACTIVE – real funds at risk ***"
            )

    # ------------------------------------------------------------------
    # Portfolio accessors used by the rest of the bot
    # ------------------------------------------------------------------

    def equity(self) -> Decimal:
        return self._portfolio.equity

    def open_position_value(self) -> Decimal:
        return self._portfolio.open_position_value

    def committed_capital(self) -> Decimal:
        return self._portfolio.committed_capital

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
        size_usdc: object,
        poly_price: object,
        cex_implied_prob: float,
        edge_pct: float,
        confidence: float,
        kelly_fraction: float,
    ) -> Position | None:
        """
        Open a position.  Returns a Position object on success, None on failure.
        """
        size = quantize_usdc(size_usdc)
        if size <= ZERO:
            logger.warning("Skipping %s %s – size %s <= 0", side, contract_key, fmt_usdc(size))
            return None

        if size > self._portfolio.cash_usdc:
            logger.warning(
                "Skipping %s %s – size %s exceeds free cash %s",
                side, contract_key, fmt_usdc(size), fmt_usdc(self._portfolio.cash_usdc),
            )
            return None

        kwargs: dict[str, Any] = dict(
            contract_key=contract_key, token_id=token_id, side=side,
            size_usdc=size, poly_price=clamp_price(poly_price),
            cex_implied_prob=cex_implied_prob, edge_pct=edge_pct,
            confidence=confidence, kelly_fraction=kelly_fraction,
        )
        if self._live:
            return await self._enter_live(**kwargs)
        return await self._enter_paper(**kwargs)

    def _record_entry(
        self,
        *,
        mode: str,
        kwargs: dict[str, Any],
        fill_price: Decimal,
        order_id: str | None,
    ) -> Position | None:
        """Size, book and persist a filled entry.  Shared by paper and live."""
        contract_key = kwargs["contract_key"]
        side         = kwargs["side"]

        shares = shares_for_cost(kwargs["size_usdc"], fill_price)
        if shares <= ZERO:
            logger.warning(
                "Skipping %s %s – budget %s buys 0 shares at %s",
                side, contract_key, fmt_usdc(kwargs["size_usdc"]), fmt_price(fill_price),
            )
            return None

        # Cost is recomputed from the quantised share count, so the balance
        # deduction matches what the exchange will actually charge rather than
        # the pre-rounding budget.
        cost = notional(shares, fill_price)

        trade_id = self._db.insert_trade(
            mode=mode,
            contract_key=contract_key,
            token_id=kwargs["token_id"],
            side=side,
            shares=shares,
            cost_usdc=cost,
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
            token_id=kwargs["token_id"],
            side=side,
            shares=shares,
            entry_price=fill_price,
            cost_usdc=cost,
            order_id=order_id,
        )
        pos.set_mark(fill_price)
        self._portfolio.add_position(pos)

        logger.info(
            "[%s] Entered %s %s  shares=%s  cost=%s USDC  price=%s%s",
            mode.upper(), side, contract_key, quantize_shares(shares),
            fmt_usdc(cost), fmt_price(fill_price),
            f"  order_id={order_id}" if order_id else "",
        )
        return pos

    async def _enter_paper(self, **kwargs: Any) -> Position | None:
        poly_price = kwargs["poly_price"]
        side       = kwargs["side"]

        penalty = Decimal(1) + (_PAPER_SLIPPAGE if side == "BUY" else -_PAPER_SLIPPAGE)
        simulated_fill = clamp_price(poly_price * penalty)

        return self._record_entry(
            mode="paper", kwargs=kwargs, fill_price=simulated_fill, order_id=None,
        )

    async def _enter_live(self, **kwargs: Any) -> Position | None:
        if not _CLOB_AVAILABLE or self._client is None:
            logger.error("Live trading requested but ClobClient not available.")
            return None

        poly_price   = kwargs["poly_price"]
        side         = kwargs["side"]
        contract_key = kwargs["contract_key"]

        clob_side = BUY if side == "BUY" else SELL
        num_tokens = shares_for_cost(kwargs["size_usdc"], poly_price)
        if num_tokens <= ZERO:
            logger.warning("Live order for %s sized to 0 shares – skipping.", contract_key)
            return None

        try:
            loop = asyncio.get_running_loop()
            order_args = MarketOrderArgs(
                token_id=kwargs["token_id"],
                amount=float(num_tokens),
                side=clob_side,
            )
            response = await loop.run_in_executor(
                None,
                lambda: self._client.create_market_order(order_args),  # type: ignore[union-attr]
            )
            order_id   = str(response.get("orderID", "unknown"))
            # The CLOB returns price as a decimal string; to_decimal keeps it
            # exact instead of bouncing it through a float.
            fill_price = clamp_price(to_decimal(response.get("price", poly_price), field="fill_price"))
        except Exception as exc:
            logger.error("Live order failed for %s: %s", contract_key, exc)
            return None

        return self._record_entry(
            mode="live", kwargs=kwargs, fill_price=fill_price, order_id=order_id,
        )

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    async def close_position(
        self,
        trade_id: int,
        exit_price: object,
    ) -> Decimal:
        """
        Close an open position at exit_price.  Returns realised PnL in USDC.
        """
        pos = self._portfolio.positions.get(trade_id)
        if pos is None:
            logger.warning("close_position: trade_id %d not found.", trade_id)
            return ZERO

        px = clamp_price(exit_price)
        pnl = pos.pnl_at(px)

        self._db.close_trade(trade_id, px, pnl)
        self._portfolio.close_position(trade_id, pnl)
        logger.info(
            "Closed trade #%d %s %s  exit=%s  PnL=%s USDC",
            trade_id, pos.side, pos.contract_key, fmt_price(px), fmt_usdc(pnl, 4),
        )
        return pnl

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def mark_positions(self, price_fn: Callable[[str], object | None]) -> None:
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
