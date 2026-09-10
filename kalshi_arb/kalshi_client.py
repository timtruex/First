"""
Kalshi V2 REST client.

Hand-rolled rather than generated: the scanner needs explicit control over
connection reuse, retry policy and rate-limit accounting, and a generated
client hides exactly those. Only V2 endpoints under /trade-api/v2 are used.

Every call goes through the weighted limiter before it touches the network, so
the budget is spent in the same order requests are sent. Reads are unsigned
where Kalshi allows it, which keeps market-data scanning usable without
credentials — useful because the scanner's research mode never needs to
authenticate.

Order mutations are gated behind DRY_RUN and refuse to send unless it is
explicitly disabled. That gate lives in this client rather than in the caller
so there is no path to a live order that bypasses it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from kalshi_auth import KalshiSigner
from orderbook import Book, book_from_kalshi
from rate_limit import WeightedLimiter

logger = logging.getLogger(__name__)

PROD_BASE = "https://api.elections.kalshi.com"
DEMO_BASE = "https://demo-api.kalshi.co"
V2 = "/trade-api/v2"


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"Kalshi {status} on {path}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


class DryRunBlocked(RuntimeError):
    """Raised when a mutation is attempted while DRY_RUN is active."""


@dataclass
class KalshiClient:
    session: aiohttp.ClientSession
    limiter: WeightedLimiter
    signer: KalshiSigner | None = None
    base_url: str = PROD_BASE
    dry_run: bool = True
    max_retries: int = 3

    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str = "read",
        params: dict | None = None,
        json_body: dict | None = None,
        signed: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if signed:
            if self.signer is None:
                raise KalshiAPIError(401, "no signer configured", path)
            headers.update(self.signer.headers(method, path))

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            await self.limiter.acquire(operation)
            try:
                async with self.session.request(
                    method, url, params=params, json=json_body, headers=headers
                ) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        # Server-side limiter disagreed with ours: back off and
                        # treat it as a signal the local weights are too low.
                        wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                        logger.warning(
                            "Kalshi 429 on %s — local rate limit is undersized; "
                            "backing off %.1fs", path, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        last_exc = KalshiAPIError(resp.status, body, path)
                        await asyncio.sleep(2 ** attempt * 0.5)
                        continue
                    if resp.status >= 400:
                        raise KalshiAPIError(resp.status, body, path)
                    return await resp.json()
            except aiohttp.ClientError as exc:
                last_exc = exc
                await asyncio.sleep(2 ** attempt * 0.5)

        raise last_exc or KalshiAPIError(0, "exhausted retries", path)

    # ------------------------------------------------------------------
    # Market data (read-only)
    # ------------------------------------------------------------------

    async def get_markets(
        self, *, limit: int = 100, cursor: str | None = None, status: str = "open",
        series_ticker: str | None = None, event_ticker: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return await self._request("GET", f"{V2}/markets", params=params)

    async def iter_markets(self, *, status: str = "open", page_size: int = 100) -> list[dict]:
        """Page through every open market, respecting the limiter throughout."""
        out: list[dict] = []
        cursor: str | None = None
        while True:
            page = await self.get_markets(limit=page_size, cursor=cursor, status=status)
            markets = page.get("markets", [])
            out.extend(markets)
            cursor = page.get("cursor")
            if not cursor or not markets:
                break
        return out

    async def get_market(self, ticker: str) -> dict:
        return await self._request("GET", f"{V2}/markets/{ticker}")

    async def get_orderbook(self, ticker: str, *, depth: int = 10) -> dict:
        return await self._request(
            "GET", f"{V2}/markets/{ticker}/orderbook", params={"depth": depth}
        )

    async def get_books(self, ticker: str, *, depth: int = 10) -> tuple[Book, Book]:
        """Fetch one market and return its (YES, NO) books, already normalised."""
        raw = await self.get_orderbook(ticker, depth=depth)
        ob = raw.get("orderbook", raw) or {}
        yes = ob.get("yes") or []
        no = ob.get("no") or []
        return (
            book_from_kalshi(ticker, "YES", yes_levels=yes, no_levels=no),
            book_from_kalshi(ticker, "NO", yes_levels=yes, no_levels=no),
        )

    # ------------------------------------------------------------------
    # Portfolio (signed)
    # ------------------------------------------------------------------

    async def get_balance(self) -> dict:
        return await self._request("GET", f"{V2}/portfolio/balance", signed=True)

    async def get_positions(self, *, limit: int = 100) -> dict:
        return await self._request(
            "GET", f"{V2}/portfolio/positions", params={"limit": limit}, signed=True
        )

    async def get_orders(self, *, status: str | None = None, limit: int = 100) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request(
            "GET", f"{V2}/portfolio/orders", params=params, signed=True
        )

    # ------------------------------------------------------------------
    # Order mutations — gated
    # ------------------------------------------------------------------

    def _assert_live(self, what: str, payload: dict) -> None:
        if self.dry_run:
            logger.info("[DRY_RUN] suppressed %s: %s", what, payload)
            raise DryRunBlocked(
                f"{what} blocked: DRY_RUN is active. Set dry_run=False explicitly "
                "on the client to send real orders."
            )

    async def create_order(
        self,
        *,
        ticker: str,
        side: str,              # "yes" | "no"
        action: str,            # "buy" | "sell"
        count: int,
        price_cents: int,
        client_order_id: str,
        order_type: str = "limit",
    ) -> dict:
        """
        Place a V2 limit order.

        `client_order_id` is required rather than optional: it is what makes a
        retry after an ambiguous network failure idempotent instead of
        doubling the position.
        """
        if count <= 0:
            raise ValueError("count must be positive")
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents must be 1..99, got {price_cents}")
        if not client_order_id:
            raise ValueError("client_order_id is required for safe retries")

        payload = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id,
            ("yes_price" if side == "yes" else "no_price"): price_cents,
        }
        self._assert_live("create_order", payload)
        return await self._request(
            "POST", f"{V2}/portfolio/orders",
            operation="order_create", json_body=payload, signed=True,
        )

    async def cancel_order(self, order_id: str) -> dict:
        self._assert_live("cancel_order", {"order_id": order_id})
        return await self._request(
            "DELETE", f"{V2}/portfolio/orders/{order_id}",
            operation="order_cancel", signed=True,
        )

    async def amend_order(self, order_id: str, *, count: int, price_cents: int) -> dict:
        payload = {"count": count, "price": price_cents}
        self._assert_live("amend_order", payload)
        return await self._request(
            "POST", f"{V2}/portfolio/orders/{order_id}/amend",
            operation="order_amend", json_body=payload, signed=True,
        )

    async def decrease_order(self, order_id: str, *, reduce_by: int) -> dict:
        payload = {"reduce_by": reduce_by}
        self._assert_live("decrease_order", payload)
        return await self._request(
            "POST", f"{V2}/portfolio/orders/{order_id}/decrease",
            operation="order_decrease", json_body=payload, signed=True,
        )
