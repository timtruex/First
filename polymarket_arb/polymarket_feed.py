"""
Polymarket CLOB orderbook monitor.

Polls the CLOB REST API for best-bid/best-ask on each configured contract
and derives a mid-price (implied probability) for each token.

The py-clob-client library is used for authenticated requests; we also
expose a raw httpx fallback for unauthenticated orderbook reads.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    ClobClient  = None   # type: ignore[assignment,misc]
    ApiCreds    = None   # type: ignore[assignment,misc]

from config import (
    CLOB_HOST,
    CLOB_POLL_INTERVAL,
    POLY_API_KEY,
    POLY_API_PASSPHRASE,
    POLY_API_SECRET,
    POLY_PRIVATE_KEY,
    CHAIN_ID,
    get_window_configs,
    WindowConfig,
)

logger = logging.getLogger(__name__)

_ORDERBOOK_PATH = "/book"          # GET /book?token_id=<id>
_MAX_RETRIES    = 3
_RETRY_BACKOFF  = 2.0              # seconds


@dataclass
class OrderbookSnapshot:
    token_id:   str
    contract_key: str
    ts:         float = field(default_factory=time.time)
    best_bid:   float = 0.0        # highest buy price (0–1)
    best_ask:   float = 1.0        # lowest  sell price (0–1)
    mid:        float = 0.5
    spread:     float = 1.0
    is_valid:   bool  = False      # False until first successful fetch

    def update(self, best_bid: float, best_ask: float) -> None:
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.mid      = (best_bid + best_ask) / 2.0
        self.spread   = best_ask - best_bid
        self.ts       = time.time()
        self.is_valid = True

    def is_stale(self, max_age: float = 10.0) -> bool:
        return (time.time() - self.ts) > max_age


class PolymarketFeed:
    """
    Polls Polymarket CLOB for orderbook data on all configured contracts.

    Falls back to unauthenticated HTTP if credentials are absent, which
    works for reading public orderbooks.
    """

    def __init__(self) -> None:
        self._configs: list[WindowConfig] = get_window_configs()
        # Build a flat map: token_id -> snapshot
        self._snapshots: dict[str, OrderbookSnapshot] = {}
        self._key_to_tokens: dict[str, tuple[str, str]] = {}

        for cfg in self._configs:
            for direction, token_id in (("UP", cfg.up_token), ("DOWN", cfg.down_token)):
                contract_key = f"{cfg.asset}_{cfg.window_min}M_{direction}"
                if token_id:
                    self._snapshots[token_id] = OrderbookSnapshot(
                        token_id=token_id,
                        contract_key=contract_key,
                    )
            self._key_to_tokens[cfg.key] = (cfg.up_token, cfg.down_token)

        self._client: ClobClient | None = self._build_client()
        self._running   = False
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _build_client(self) -> ClobClient | None:
        if not _CLOB_AVAILABLE:
            logger.warning("py-clob-client not installed – using raw HTTP fallback.")
            return None
        if not POLY_PRIVATE_KEY:
            logger.warning("No POLY_PRIVATE_KEY – using unauthenticated CLOB reads.")
            return None
        try:
            creds = ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_API_SECRET,
                api_passphrase=POLY_API_PASSPHRASE,
            )
            client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                creds=creds,
            )
            logger.info("ClobClient initialised (authenticated).")
            return client
        except Exception as exc:
            logger.error("Failed to build ClobClient: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self._snapshots.get(token_id)

    def get_mid(self, token_id: str) -> float | None:
        snap = self._snapshots.get(token_id)
        if snap is None or not snap.is_valid:
            return None
        return snap.mid

    def get_contract_mid(self, contract_key: str) -> float | None:
        """Look up mid by contract_key string like 'BTC_5M_UP'."""
        for snap in self._snapshots.values():
            if snap.contract_key == contract_key:
                return snap.mid if snap.is_valid else None
        return None

    def all_snapshots(self) -> list[OrderbookSnapshot]:
        return list(self._snapshots.values())

    def is_healthy(self) -> bool:
        valid = [s for s in self._snapshots.values() if s.is_valid]
        return len(valid) == len(self._snapshots) and all(
            not s.is_stale() for s in valid
        )

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        logger.info("Polymarket CLOB poll loop started (interval=%.1fs).", CLOB_POLL_INTERVAL)
        while self._running:
            try:
                await self._fetch_all()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Poll loop error: %s", exc)
            await asyncio.sleep(CLOB_POLL_INTERVAL)

    async def _fetch_all(self) -> None:
        """Fetch orderbooks for every token concurrently."""
        token_ids = [tid for tid in self._snapshots if tid]
        if not token_ids:
            logger.debug("No token IDs configured – skipping CLOB poll.")
            return
        tasks = [self._fetch_orderbook(tid) for tid in token_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_orderbook(self, token_id: str) -> None:
        """Fetch a single orderbook and update the snapshot."""
        snap = self._snapshots.get(token_id)
        if snap is None:
            return

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                data = await self._get_book(token_id)
                self._parse_book(snap, data)
                logger.debug(
                    "[CLOB] %s bid=%.4f ask=%.4f mid=%.4f",
                    snap.contract_key, snap.best_bid, snap.best_ask, snap.mid,
                )
                return
            except Exception as exc:
                if attempt == _MAX_RETRIES:
                    logger.warning(
                        "Failed to fetch orderbook for %s after %d attempts: %s",
                        snap.contract_key, _MAX_RETRIES, exc,
                    )
                else:
                    await asyncio.sleep(_RETRY_BACKOFF * attempt)

    async def _get_book(self, token_id: str) -> dict[str, Any]:
        """Retrieve orderbook data using py-clob-client or raw HTTP."""
        if self._client is not None:
            # Run sync SDK call in a thread to not block event loop
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None, self._client.get_order_book, token_id
            )
            # py-clob-client returns an OrderBookSummary or dict
            if hasattr(data, "__dict__"):
                return data.__dict__
            return data  # type: ignore[return-value]

        # Unauthenticated fallback
        url = f"{CLOB_HOST}{_ORDERBOOK_PATH}"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params={"token_id": token_id})
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _parse_book(snap: OrderbookSnapshot, data: dict[str, Any]) -> None:
        """
        Extract best bid/ask from either py-clob-client format or raw JSON.

        CLOB book JSON shape (REST endpoint):
        {
          "market": "...",
          "asset_id": "...",
          "bids": [{"price": "0.54", "size": "100"}, ...],
          "asks": [{"price": "0.56", "size": "100"}, ...]
        }
        py-clob-client may return attributes directly.
        """
        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []

        if not bids_raw and not asks_raw:
            logger.debug("Empty book for %s", snap.contract_key)
            return

        def best_bid(bids: list) -> float:
            prices = []
            for b in bids:
                if isinstance(b, dict):
                    prices.append(float(b.get("price", 0)))
                else:
                    prices.append(float(getattr(b, "price", 0)))
            return max(prices) if prices else 0.0

        def best_ask(asks: list) -> float:
            prices = []
            for a in asks:
                if isinstance(a, dict):
                    prices.append(float(a.get("price", 1)))
                else:
                    prices.append(float(getattr(a, "price", 1)))
            return min(prices) if prices else 1.0

        snap.update(best_bid(bids_raw), best_ask(asks_raw))
