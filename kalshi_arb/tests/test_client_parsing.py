"""
Client response handling, without network.

The live endpoints are not reachable from CI, so these cover the parts that
are ours: normalising each venue's book shape and the retry/backoff policy.
The wire format itself is unverified against the live API.
"""
import asyncio
from decimal import Decimal

import pytest

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from rate_limit import WeightedLimiter


def run(coro):
    return asyncio.run(coro)


def kalshi(responses):
    c = KalshiClient(session=None, limiter=WeightedLimiter(capacity=1000, refill_per_second=1000))
    calls = []

    async def fake(method, path, **kw):
        calls.append((method, path, kw))
        return responses.pop(0)

    c._request = fake
    c.calls = calls
    return c


def test_kalshi_books_normalise_both_outcomes():
    """
    Kalshi publishes bids on both sides only. YES ask must be derived from the
    NO bids as (1 - price); the inverse for the NO book.
    """
    c = kalshi([{"orderbook": {"yes": [[40, 100], [39, 50]], "no": [[58, 80]]}}])
    yes, no = run(c.get_books("TICKER"))

    assert yes.best_bid == Decimal("0.4000")
    assert yes.best_ask == Decimal("0.4200")     # 1 - 0.58
    assert no.best_bid == Decimal("0.5800")
    assert no.best_ask == Decimal("0.6000")      # 1 - 0.40 (best yes bid)


def test_kalshi_books_handle_unwrapped_payload():
    """Some responses omit the 'orderbook' envelope."""
    c = kalshi([{"yes": [[40, 100]], "no": [[58, 80]]}])
    yes, _ = run(c.get_books("T"))
    assert yes.best_ask == Decimal("0.4200")


def test_kalshi_books_handle_empty_and_null_sides():
    for payload in ({"orderbook": {"yes": [], "no": []}},
                    {"orderbook": {"yes": None, "no": None}},
                    {"orderbook": None}):
        c = kalshi([payload])
        yes, no = run(c.get_books("T"))
        assert yes.is_empty and no.is_empty


def test_zero_size_levels_are_dropped():
    c = kalshi([{"orderbook": {"yes": [[40, 0], [39, 25]], "no": [[58, 0]]}}])
    yes, _ = run(c.get_books("T"))
    assert yes.best_bid == Decimal("0.3900")
    assert yes.best_ask is None


def test_levels_sort_best_first_regardless_of_input_order():
    c = kalshi([{"orderbook": {"yes": [[35, 10], [41, 10], [38, 10]], "no": [[55, 5], [60, 5]]}}])
    yes, _ = run(c.get_books("T"))
    assert [l.price for l in yes.bids] == [Decimal("0.41"), Decimal("0.38"), Decimal("0.35")]
    assert yes.best_ask == Decimal("0.4000")     # 1 - 0.60, the best offer


def test_iter_markets_pages_until_cursor_exhausted():
    c = kalshi([
        {"markets": [{"ticker": "A"}], "cursor": "c1"},
        {"markets": [{"ticker": "B"}], "cursor": "c2"},
        {"markets": [], "cursor": None},
    ])
    assert [m["ticker"] for m in run(c.iter_markets())] == ["A", "B"]


def test_iter_markets_stops_on_missing_cursor():
    c = kalshi([{"markets": [{"ticker": "A"}]}])
    assert len(run(c.iter_markets())) == 1


def test_signed_endpoints_request_signing():
    c = kalshi([{"balance": 1234}])
    run(c.get_balance())
    _, path, kw = c.calls[0]
    assert path == "/trade-api/v2/portfolio/balance"
    assert kw["signed"] is True


def test_reads_are_unsigned():
    c = kalshi([{"markets": []}])
    run(c.get_markets())
    assert c.calls[0][2].get("signed", False) is False


def test_mutations_charge_the_order_weight():
    c = KalshiClient(
        session=None,
        limiter=WeightedLimiter(capacity=1000, refill_per_second=1000),
        dry_run=False,
    )
    seen = {}

    async def fake(method, path, *, operation="read", **kw):
        seen["operation"] = operation
        return {}

    c._request = fake
    run(c.create_order(ticker="T", side="yes", action="buy", count=1,
                       price_cents=40, client_order_id="cid"))
    assert seen["operation"] == "order_create"


# ----------------------------------------------------------------------
# Polymarket
# ----------------------------------------------------------------------

def polymarket(responses):
    c = PolymarketClient(session=None, limiter=WeightedLimiter(capacity=1000, refill_per_second=1000))

    async def fake(base, path, **kw):
        return responses.pop(0)

    c._get = fake
    return c


def test_polymarket_book_parsing():
    c = polymarket([{
        "bids": [{"price": "0.44", "size": "120"}, {"price": "0.43", "size": "80"}],
        "asks": [{"price": "0.46", "size": "90"}, {"price": "0.47", "size": "60"}],
    }])
    book = run(c.get_book("token", "YES"))
    assert book.best_bid == Decimal("0.4400")
    assert book.best_ask == Decimal("0.4600")
    assert book.cost_to_buy(Decimal(150))[1] == Decimal("0.4640")


def test_polymarket_empty_book():
    c = polymarket([{"bids": [], "asks": []}])
    assert run(c.get_book("t", "YES")).is_empty


def test_polymarket_fetches_both_legs_concurrently():
    """
    Serial fetches price the two legs at different instants, which shows up as
    phantom edge on a moving market.
    """
    order = []

    c = PolymarketClient(session=None,
                         limiter=WeightedLimiter(capacity=1000, refill_per_second=1000))

    async def fake_book(token_id, outcome="YES"):
        order.append(f"start-{outcome}")
        await asyncio.sleep(0.01)
        order.append(f"end-{outcome}")
        from orderbook import book_from_polymarket
        return book_from_polymarket(token_id, outcome, bids=[], asks=[])

    c.get_book = fake_book
    run(c.get_books("yes-tok", "no-tok"))
    # Both start before either finishes.
    assert order[:2] == ["start-YES", "start-NO"]


def test_iter_markets_stops_on_short_page():
    c = polymarket([[{"id": 1}, {"id": 2}]])
    assert len(run(c.iter_markets(page_size=10))) == 2
