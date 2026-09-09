"""
Position and portfolio accounting exactness.

Includes a reproduction of the pre-fix float drift so the regression these
tests guard against is documented rather than asserted on faith.
"""
from decimal import Decimal

import pytest

from money import ZERO, quantize_usdc, notional, shares_for_cost
from trader import Position, PortfolioState


def make_pos(side="BUY", budget="100", price="0.37", trade_id=1) -> Position:
    px = Decimal(price)
    shares = shares_for_cost(budget, px)
    return Position(
        trade_id=trade_id, contract_key="BTC_5M_UP", token_id="tok",
        side=side, shares=shares, entry_price=px,
        cost_usdc=notional(shares, px),
    )


# ----------------------------------------------------------------------
# The defect being fixed
# ----------------------------------------------------------------------

def test_float_pnl_accumulation_drifts_but_decimal_does_not():
    """
    The concrete pre-fix failure mode. A single float round-trip through
    `size/price*price` happens to be exact in IEEE754, which is what makes
    this bug easy to miss — the error appears when PnL is *accumulated*,
    which is exactly what the daily total and the kill-switch read.
    """
    size, entry, exit_ = 33.33, 0.3333, 0.3401
    float_total = 0.0
    for _ in range(10_000):
        float_total += (size / entry) * (exit_ - entry)
    assert float_total != 6800.0, "expected float drift when accumulating PnL"

    pos = make_pos(budget=str(size), price=str(entry))
    exact_total = ZERO
    per_trade = pos.pnl_at(exit_)
    for _ in range(10_000):
        exact_total += per_trade
    assert exact_total == per_trade * 10_000


def test_flat_close_returns_exactly_zero():
    for price in ("0.37", "0.0001", "0.9999", "0.3333", "0.6667"):
        for side in ("BUY", "SELL"):
            pos = make_pos(side=side, price=price)
            assert pos.pnl_at(price) == ZERO, f"{side} @ {price}"


# ----------------------------------------------------------------------
# Directional correctness
# ----------------------------------------------------------------------

def test_buy_gains_when_price_rises():
    pos = make_pos(side="BUY", price="0.40")
    assert pos.pnl_at("0.50") > ZERO
    assert pos.pnl_at("0.30") < ZERO


def test_sell_gains_when_price_falls():
    pos = make_pos(side="SELL", price="0.40")
    assert pos.pnl_at("0.30") > ZERO
    assert pos.pnl_at("0.50") < ZERO


def test_mark_defaults_to_entry_before_first_tick():
    """The old current_value raised AttributeError if set_mark never ran."""
    pos = make_pos()
    assert pos.mark_price is None
    assert pos.effective_mark == pos.entry_price
    assert pos.unrealised_pnl == ZERO
    assert pos.current_value > ZERO


# ----------------------------------------------------------------------
# Portfolio conservation
# ----------------------------------------------------------------------

def test_cash_is_conserved_across_open_and_flat_close():
    book = PortfolioState(cash_usdc=Decimal("1000"))
    start = book.equity
    pos = make_pos()
    book.add_position(pos)
    assert book.cash_usdc == quantize_usdc(Decimal("1000") - pos.cost_usdc)
    book.close_position(pos.trade_id, pos.pnl_at(pos.entry_price))
    assert book.cash_usdc == Decimal("1000")
    assert book.equity == start


def test_equity_stable_over_many_open_close_cycles():
    """
    The kill-switch reads equity. If rounding drifted in our favour across
    cycles it could mask a real drawdown.
    """
    book = PortfolioState(cash_usdc=Decimal("1000"))
    for i in range(500):
        pos = make_pos(trade_id=i, budget="33.33", price="0.3333")
        book.add_position(pos)
        book.close_position(i, pos.pnl_at(pos.entry_price))
    assert book.cash_usdc == Decimal("1000")


def test_realised_pnl_moves_cash_by_exactly_that_amount():
    book = PortfolioState(cash_usdc=Decimal("1000"))
    pos = make_pos()
    book.add_position(pos)
    pnl = pos.pnl_at("0.50")
    book.close_position(pos.trade_id, pnl)
    assert book.cash_usdc == quantize_usdc(Decimal("1000") + pnl)


def test_open_position_value_sums_exactly():
    book = PortfolioState(cash_usdc=Decimal("1000"))
    for i in range(20):
        book.add_position(make_pos(trade_id=i, budget="10", price="0.3333"))
    expected = quantize_usdc(sum((p.current_value for p in book.positions.values()), ZERO))
    assert book.open_position_value == expected
    assert book.committed_capital == quantize_usdc(
        sum((p.cost_usdc for p in book.positions.values()), ZERO)
    )
