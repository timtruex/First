"""
Polymarket REST client (Gamma metadata + CLOB books).

Read-only by design. The scanner's job is to find and price cross-venue
spreads; placing the Polymarket leg means on-chain order signing with a funded
wallet, which is a separate trust boundary and is deliberately not in this
module. Nothing here can move funds.

Two endpoints matter:

  Gamma (gamma-api.polymarket.com)  market metadata: question text, outcomes,
                                    token ids, resolution source, end date
  CLOB  (clob.polymarket.com)       live order books keyed by token id

Token ids, not condition ids, key the books: one market has two outcome tokens
(YES and NO) and each has its own book.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from orderbook import Book, book_from_polymarket
from rate_limit import WeightedLimiter

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketAPIError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"Polymarket {status} on {path}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


@dataclass
class PolymarketClient:
    session: aiohttp.ClientSession
    limiter: WeightedLimiter
    gamma_base: str = GAMMA_BASE
    clob_base: str = CLOB_BASE
    max_retries: int = 3

    async def _get(self, base: str, path: str, *, params: dict | None = None) -> Any:
        url = f"{base}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            await self.limiter.acquire("read")
            try:
                async with self.session.get(
                    url, params=params, headers={"Accept": "application/json"}
                ) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                        logger.warning("Polymarket 429 on %s — backing off %.1fs", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        last_exc = PolymarketAPIError(resp.status, body, path)
                        await asyncio.sleep(2 ** attempt * 0.5)
                        continue
                    if resp.status >= 400:
                        raise PolymarketAPIError(resp.status, body, path)
                    return await resp.json()
            except aiohttp.ClientError as exc:
                last_exc = exc
                await asyncio.sleep(2 ** attempt * 0.5)
        raise last_exc or PolymarketAPIError(0, "exhausted retries", path)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def get_markets(
        self, *, limit: int = 100, offset: int = 0, active: bool = True, closed: bool = False
    ) -> list[dict]:
        return await self._get(
            self.gamma_base, "/markets",
            params={
                "limit": limit, "offset": offset,
                "active": str(active).lower(), "closed": str(closed).lower(),
            },
        )

    async def iter_markets(self, *, page_size: int = 100, max_pages: int = 50) -> list[dict]:
        out: list[dict] = []
        for page in range(max_pages):
            batch = await self.get_markets(limit=page_size, offset=page * page_size)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    async def get_market(self, condition_id: str) -> dict:
        markets = await self._get(
            self.gamma_base, "/markets", params={"condition_ids": condition_id}
        )
        if not markets:
            raise PolymarketAPIError(404, f"no market {condition_id}", "/markets")
        return markets[0]

    # ------------------------------------------------------------------
    # Books
    # ------------------------------------------------------------------

    async def get_book(self, token_id: str, outcome: str = "YES") -> Book:
        raw = await self._get(self.clob_base, "/book", params={"token_id": token_id})
        return book_from_polymarket(
            token_id, outcome,
            bids=[(lvl["price"], lvl["size"]) for lvl in raw.get("bids", [])],
            asks=[(lvl["price"], lvl["size"]) for lvl in raw.get("asks", [])],
        )

    async def get_books(self, yes_token: str, no_token: str) -> tuple[Book, Book]:
        """
        Fetch both outcome books concurrently.

        Concurrently rather than in sequence because the two legs are priced
        against each other: a serial fetch prices YES and NO at different
        instants, which shows up as phantom edge on a moving market.
        """
        yes, no = await asyncio.gather(
            self.get_book(yes_token, "YES"),
            self.get_book(no_token, "NO"),
        )
        return yes, no
