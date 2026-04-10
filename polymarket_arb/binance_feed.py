"""
Binance real-time price feed via WebSocket.

Subscribes to the combined stream endpoint so we need only one connection
for all symbols.  Exposes latest mid-prices and derived short-window
directional probabilities to the rest of the bot.

Price model
-----------
For each asset we track a configurable rolling window (5 min and 15 min).
A naive but fast probability estimate is:

    P(up | window) = sigmoid( z-score of return over window )

This is not intended to be a calibrated model – it exists purely to derive
an *implied* probability that Polymarket prices should converge toward, so
we can detect when Polymarket significantly *lags* the CEX.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed

from config import BINANCE_SYMBOLS, BINANCE_WS_BASE

logger = logging.getLogger(__name__)

# How many seconds of price ticks to hold per asset
_WINDOW_SECONDS = {5: 5 * 60, 15: 15 * 60}

# Reconnect back-off (seconds)
_RECONNECT_BASE   = 2
_RECONNECT_MAX    = 60
_RECONNECT_FACTOR = 2


@dataclass
class PriceTick:
    ts: float       # unix timestamp (seconds)
    price: float    # mid-price from best bid/ask or last trade


@dataclass
class AssetState:
    symbol: str                            # e.g. "BTCUSDT"
    ticks: deque[PriceTick] = field(default_factory=lambda: deque(maxlen=10_000))
    latest_price: float = 0.0
    last_update: float = 0.0

    def push(self, price: float) -> None:
        now = time.time()
        self.latest_price = price
        self.last_update  = now
        self.ticks.append(PriceTick(ts=now, price=price))

    # ------------------------------------------------------------------
    # Probability estimate
    # ------------------------------------------------------------------

    def _ticks_in_window(self, window_sec: int) -> list[PriceTick]:
        cutoff = time.time() - window_sec
        # Deque is ordered oldest→newest; slice from the right
        result = [t for t in self.ticks if t.ts >= cutoff]
        return result

    def implied_up_prob(self, window_min: int) -> float | None:
        """
        Return P(price_now > price_{window_min ago}) as a sigmoid-smoothed
        probability.  Returns None if we do not yet have enough data.
        """
        window_sec = _WINDOW_SECONDS.get(window_min)
        if window_sec is None:
            raise ValueError(f"Unsupported window: {window_min}m")

        ticks = self._ticks_in_window(window_sec)
        if len(ticks) < 2:
            return None

        price_then = ticks[0].price
        price_now  = self.latest_price

        if price_then == 0:
            return None

        log_return = math.log(price_now / price_then)

        # Approximate volatility from the window ticks for z-score scaling.
        # Use the std-dev of log returns between consecutive ticks.
        log_rets = [
            math.log(ticks[i].price / ticks[i - 1].price)
            for i in range(1, len(ticks))
            if ticks[i - 1].price > 0
        ]
        if not log_rets:
            return None

        mean_lr = sum(log_rets) / len(log_rets)
        variance = sum((r - mean_lr) ** 2 for r in log_rets) / len(log_rets)
        std_lr   = math.sqrt(variance) if variance > 0 else 1e-8

        # z-score of the total window return
        z = log_return / (std_lr * math.sqrt(len(log_rets)))

        # Map to probability via logistic sigmoid
        prob_up = 1.0 / (1.0 + math.exp(-z))
        return prob_up

    def is_stale(self, max_age_sec: float = 10.0) -> bool:
        if self.last_update == 0:
            return True
        return (time.time() - self.last_update) > max_age_sec


class BinanceFeed:
    """
    Maintains a persistent WebSocket connection to Binance combined streams
    and keeps AssetState objects up-to-date.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_price_update: Callable[[str, float], None] | None = None,
    ) -> None:
        self._symbols: list[str] = [s.lower() for s in (symbols or BINANCE_SYMBOLS)]
        self._states: dict[str, AssetState] = {
            sym: AssetState(symbol=sym.upper()) for sym in self._symbols
        }
        self._on_update = on_price_update
        self._running   = False
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_state(self, symbol: str) -> AssetState | None:
        return self._states.get(symbol.lower())

    def get_price(self, symbol: str) -> float | None:
        state = self.get_state(symbol)
        if state is None or state.last_update == 0:
            return None
        return state.latest_price

    def get_implied_prob(self, symbol: str, window_min: int) -> float | None:
        state = self.get_state(symbol)
        return state.implied_up_prob(window_min) if state else None

    def is_healthy(self) -> bool:
        return all(
            not s.is_stale() for s in self._states.values()
        )

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # WebSocket logic
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        streams = "/".join(f"{sym}@bookTicker" for sym in self._symbols)
        return f"{BINANCE_WS_BASE}?streams={streams}"

    async def _run_forever(self) -> None:
        backoff = _RECONNECT_BASE
        while self._running:
            try:
                await self._connect()
                backoff = _RECONNECT_BASE  # reset on clean run
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "Binance WS error: %s – reconnecting in %ds", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _RECONNECT_FACTOR, _RECONNECT_MAX)

    async def _connect(self) -> None:
        url = self._build_url()
        logger.info("Connecting to Binance stream: %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("Binance WebSocket connected.")
            async for raw in ws:
                if not self._running:
                    return
                try:
                    self._handle_message(raw)
                except Exception as exc:
                    logger.debug("Message parse error: %s", exc)

    def _handle_message(self, raw: str) -> None:
        """Parse a combined stream message and update state."""
        msg = json.loads(raw)
        # Combined stream wraps each event: {"stream": "btcusdt@bookTicker", "data": {...}}
        data = msg.get("data", msg)
        stream = msg.get("stream", "")

        # bookTicker fields: s=symbol, b=bestBid, a=bestAsk
        sym    = data.get("s", "").lower()
        bid    = data.get("b")
        ask    = data.get("a")

        if not sym or bid is None or ask is None:
            return

        mid = (float(bid) + float(ask)) / 2.0
        if sym not in self._states:
            return

        self._states[sym].push(mid)
        logger.debug("[Binance] %s mid=%.4f", sym.upper(), mid)

        if self._on_update:
            self._on_update(sym, mid)
