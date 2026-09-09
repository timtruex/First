"""End-to-end paper-mode lifecycle through the real Trader and Database."""
import asyncio
from decimal import Decimal

import pytest

from database import Database
from money import ZERO, quantize_usdc
from trader import Trader, _PAPER_SLIPPAGE


@pytest.fixture
def trader(tmp_path):
    return Trader(Database(path=tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


ENTRY = dict(
    contract_key="BTC_5M_UP", token_id="tok", side="BUY",
    cex_implied_prob=0.55, edge_pct=3.0, confidence=90.0, kelly_fraction=0.5,
)


def test_paper_entry_debits_cash_by_exact_fill_cost(trader):
    start = trader.equity()
    pos = run(trader.enter(size_usdc=Decimal("100"), poly_price=Decimal("0.37"), **ENTRY))
    assert pos is not None
    assert trader._portfolio.cash_usdc == quantize_usdc(start - pos.cost_usdc)
    # Equity is unchanged at entry: cash out, position value in.
    assert trader.equity() == start


def test_paper_fill_includes_slippage_penalty(trader):
    pos = run(trader.enter(size_usdc=Decimal("100"), poly_price=Decimal("0.37"), **ENTRY))
    assert pos.entry_price > Decimal("0.37"), "BUY should fill worse than mid"
    expected = (Decimal("0.37") * (Decimal(1) + _PAPER_SLIPPAGE)).quantize(Decimal("0.0001"))
    assert pos.entry_price == expected


def test_full_lifecycle_conserves_cash_on_flat_close(trader):
    start = trader.equity()
    pos = run(trader.enter(size_usdc=Decimal("100"), poly_price=Decimal("0.37"), **ENTRY))
    pnl = run(trader.close_position(pos.trade_id, pos.entry_price))
    assert pnl == ZERO
    assert trader.equity() == start
    assert trader.get_open_positions() == []


def test_winning_close_credits_exact_pnl(trader):
    start = trader.equity()
    pos = run(trader.enter(size_usdc=Decimal("100"), poly_price=Decimal("0.37"), **ENTRY))
    pnl = run(trader.close_position(pos.trade_id, Decimal("0.50")))
    assert pnl > ZERO
    assert trader.equity() == quantize_usdc(start + pnl)
    assert trader._db.get_daily_pnl() == pnl


def test_entry_rejected_when_size_exceeds_cash(trader):
    huge = trader._portfolio.cash_usdc + Decimal("1")
    assert run(trader.enter(size_usdc=huge, poly_price=Decimal("0.37"), **ENTRY)) is None
    assert trader.get_open_positions() == []


def test_zero_and_negative_size_rejected(trader):
    for size in (ZERO, Decimal("-10")):
        assert run(trader.enter(size_usdc=size, poly_price=Decimal("0.37"), **ENTRY)) is None


def test_dust_budget_that_buys_no_shares_is_rejected(trader):
    """A budget below one share-quantum must not open a zero-share position."""
    pos = run(trader.enter(size_usdc=Decimal("0.0000001"), poly_price=Decimal("0.9999"), **ENTRY))
    assert pos is None
    assert trader.get_open_positions() == []


def test_extreme_prices_do_not_raise(trader):
    for px in (Decimal("0"), Decimal("1"), Decimal("-1"), Decimal("2")):
        p = run(trader.enter(size_usdc=Decimal("10"), poly_price=px, **ENTRY))
        if p is not None:
            run(trader.close_position(p.trade_id, px))


def test_many_cycles_leave_equity_exact(trader):
    """500 round-trips at a flat price must return the book to its start."""
    start = trader.equity()
    for _ in range(500):
        pos = run(trader.enter(size_usdc=Decimal("33.33"), poly_price=Decimal("0.3333"), **ENTRY))
        assert pos is not None
        run(trader.close_position(pos.trade_id, pos.entry_price))
    assert trader.equity() == start
    assert trader._db.get_daily_pnl() == ZERO


def test_close_unknown_trade_is_inert(trader):
    assert run(trader.close_position(9999, Decimal("0.5"))) == ZERO


def test_mark_positions_updates_unrealised_only(trader):
    start = trader.equity()
    pos = run(trader.enter(size_usdc=Decimal("100"), poly_price=Decimal("0.37"), **ENTRY))
    trader.mark_positions(lambda _tok: Decimal("0.50"))
    assert pos.mark_price == Decimal("0.50")
    assert pos.unrealised_pnl > ZERO
    assert trader.equity() > start
    # Marking does not touch cash.
    assert trader._portfolio.cash_usdc == quantize_usdc(start - pos.cost_usdc)
