"""Kelly sizing: exact dollars out, risk caps respected."""
from decimal import Decimal

import pytest

from arbitrage_engine import ArbitrageEngine, compute_drawdown, is_kill_switch_triggered
from config import MAX_POSITION_FRACTION
from money import ZERO, USDC_QUANTUM


def engine(open_value=ZERO):
    return ArbitrageEngine(
        binance=None, polymarket=None,
        portfolio_equity_fn=lambda: Decimal("1000"),
        open_position_value_fn=lambda: open_value,
    )


def test_size_is_decimal_and_cent_exact():
    size = engine()._kelly_size(Decimal("0.40"), 0.60, Decimal("1000"), "BUY")
    assert isinstance(size, Decimal)
    assert size == size.quantize(USDC_QUANTUM)
    assert size > ZERO


def test_no_edge_means_no_position():
    """p == price is a zero-edge bet; Kelly must return nothing."""
    assert engine()._kelly_size(Decimal("0.50"), 0.50, Decimal("1000"), "BUY") == ZERO


def test_negative_edge_never_sizes_negative():
    size = engine()._kelly_size(Decimal("0.60"), 0.30, Decimal("1000"), "BUY")
    assert size == ZERO


def test_size_capped_at_max_position_fraction():
    equity = Decimal("1000")
    # Near-certain edge would otherwise size enormous under full Kelly.
    size = engine()._kelly_size(Decimal("0.10"), 0.99, equity, "BUY")
    assert size <= equity * Decimal(str(MAX_POSITION_FRACTION))


def test_existing_exposure_reduces_remaining_budget():
    equity = Decimal("1000")
    cap = equity * Decimal(str(MAX_POSITION_FRACTION))
    full = engine()._kelly_size(Decimal("0.10"), 0.99, equity, "BUY")
    partial = engine(open_value=cap / 2)._kelly_size(Decimal("0.10"), 0.99, equity, "BUY")
    assert partial < full
    exhausted = engine(open_value=cap)._kelly_size(Decimal("0.10"), 0.99, equity, "BUY")
    assert exhausted == ZERO


def test_over_allocated_book_never_returns_negative_size():
    cap = Decimal("1000") * Decimal(str(MAX_POSITION_FRACTION))
    size = engine(open_value=cap * 3)._kelly_size(Decimal("0.10"), 0.99, Decimal("1000"), "BUY")
    assert size == ZERO


def test_extreme_prices_do_not_divide_by_zero():
    for px in ("0.0001", "0.9999"):
        for side in ("BUY", "SELL"):
            size = engine()._kelly_size(Decimal(px), 0.5, Decimal("1000"), side)
            assert size >= ZERO


def test_zero_equity_sizes_nothing():
    assert engine()._kelly_size(Decimal("0.40"), 0.60, ZERO, "BUY") == ZERO


def test_filters_reject_zero_and_negative_equity():
    eng = engine()
    ok, reason = eng._check_filters(
        lag_pct=99.0, edge_pct=99.0, confidence=99.0,
        kelly_size=Decimal("10"), equity=ZERO,
    )
    assert not ok and "equity" in reason


# ----------------------------------------------------------------------
# Drawdown / kill switch
# ----------------------------------------------------------------------

def test_drawdown_exact_inputs_float_output():
    assert compute_drawdown(Decimal("800"), Decimal("1000")) == pytest.approx(0.20)
    assert compute_drawdown(Decimal("1200"), Decimal("1000")) == 0.0
    assert compute_drawdown(Decimal("500"), ZERO) == 0.0


def test_kill_switch_fires_at_threshold():
    triggered, dd = is_kill_switch_triggered(Decimal("700"), Decimal("1000"))
    assert triggered and dd == pytest.approx(0.30)
    triggered, _ = is_kill_switch_triggered(Decimal("995"), Decimal("1000"))
    assert not triggered
