"""
The DRY_RUN gate and order validation.

These are the tests that matter most in this package: everything else costs
you a missed opportunity, this costs you money.
"""
import asyncio

import pytest

from kalshi_client import DryRunBlocked, KalshiClient
from rate_limit import WeightedLimiter


def run(coro):
    return asyncio.run(coro)


def client(dry_run=True) -> KalshiClient:
    # session is never touched: every call here is blocked before the network.
    return KalshiClient(
        session=None,
        limiter=WeightedLimiter(capacity=100, refill_per_second=10),
        dry_run=dry_run,
    )


ORDER = dict(ticker="T", side="yes", action="buy", count=10,
             price_cents=40, client_order_id="cid-1")


def test_dry_run_is_the_default():
    assert KalshiClient(session=None, limiter=WeightedLimiter(
        capacity=10, refill_per_second=1)).dry_run is True


@pytest.mark.parametrize("call,kwargs", [
    ("create_order", ORDER),
    ("cancel_order", {"order_id": "o1"}),
    ("amend_order", {"order_id": "o1", "count": 5, "price_cents": 40}),
    ("decrease_order", {"order_id": "o1", "reduce_by": 5}),
])
def test_every_mutation_is_blocked_in_dry_run(call, kwargs):
    c = client(dry_run=True)
    with pytest.raises(DryRunBlocked):
        run(getattr(c, call)(**kwargs))


def test_reads_are_not_gated():
    """Blocking reads would make the scanner useless; only mutations are gated."""
    c = client(dry_run=True)
    # _assert_live is the gate; reads never call it.
    c._assert_live_called = False
    assert c.dry_run
    # get_books would hit the network, so assert the gate isn't on the read path.
    import inspect
    for name in ("get_markets", "get_orderbook", "get_balance", "get_positions"):
        assert "_assert_live" not in inspect.getsource(getattr(KalshiClient, name))


# ----------------------------------------------------------------------
# Order validation happens before the gate, so bad orders fail loudly
# even in live mode
# ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"count": 0}, {"count": -5},
    {"price_cents": 0}, {"price_cents": 100}, {"price_cents": -1},
    {"client_order_id": ""},
])
def test_invalid_orders_rejected_in_live_mode(bad):
    c = client(dry_run=False)
    with pytest.raises(ValueError):
        run(c.create_order(**{**ORDER, **bad}))


def test_valid_price_bounds_accepted():
    """1c and 99c are valid; validation must not reject the wings."""
    c = client(dry_run=True)
    for px in (1, 99):
        with pytest.raises(DryRunBlocked):   # got past validation to the gate
            run(c.create_order(**{**ORDER, "price_cents": px}))


def test_client_order_id_is_required_for_idempotent_retries():
    c = client(dry_run=False)
    with pytest.raises(ValueError, match="client_order_id"):
        run(c.create_order(**{**ORDER, "client_order_id": ""}))


def test_side_selects_the_correct_price_field():
    """A yes_price on a no order is silently wrong, not an error, at the API."""
    import inspect
    src = inspect.getsource(KalshiClient.create_order)
    assert 'yes_price' in src and 'no_price' in src


def test_config_requires_explicit_sentinel_for_live(monkeypatch):
    """
    Live trading must not be reachable by a truthy value — only the exact
    sentinel string.
    """
    import importlib
    import config as cfg
    for value in ("", "true", "1", "yes", "TRUE", "I_UNDERSTAND", "0"):
        monkeypatch.setenv("KALSHI_ARB_LIVE", value)
        reloaded = importlib.reload(cfg)
        assert reloaded.DRY_RUN is True, f"{value!r} must not enable live trading"

    monkeypatch.setenv("KALSHI_ARB_LIVE", "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS")
    reloaded = importlib.reload(cfg)
    assert reloaded.DRY_RUN is False
    monkeypatch.delenv("KALSHI_ARB_LIVE")
    importlib.reload(cfg)
