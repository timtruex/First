"""Fee models match published formulas and behave at the wings."""
from decimal import Decimal

import pytest

from fees import KalshiFees, PolymarketFees
from money import ZERO


def test_matches_published_kalshi_example():
    """0.07 * 100 * 0.50 * 0.50 = $1.75 per 100 contracts at 50c."""
    assert KalshiFees().trade_fee(100, Decimal("0.50")) == Decimal("1.75")


def test_fee_peaks_at_the_midpoint():
    f = KalshiFees()
    mid = f.trade_fee(100, Decimal("0.50"))
    for px in ("0.10", "0.25", "0.75", "0.90"):
        assert f.trade_fee(100, Decimal(px)) < mid


def test_fee_is_symmetric_around_fifty_cents():
    f = KalshiFees()
    assert f.trade_fee(100, Decimal("0.30")) == f.trade_fee(100, Decimal("0.70"))


def test_fee_always_rounds_up_to_a_whole_cent():
    fee = KalshiFees().trade_fee(3, Decimal("0.50"))
    assert fee == fee.quantize(Decimal("0.01"))
    assert fee > ZERO, "a tiny order still owes at least a cent"


def test_zero_and_negative_counts_are_free():
    f = KalshiFees()
    assert f.trade_fee(0, Decimal("0.5")) == ZERO
    assert f.trade_fee(-5, Decimal("0.5")) == ZERO


def test_settlement_fee_is_added_to_the_round_trip():
    plain = KalshiFees()
    with_settle = KalshiFees(settlement_fee_per_contract=Decimal("0.01"))
    assert (with_settle.round_trip_to_settlement(100, Decimal("0.5"))
            == plain.round_trip_to_settlement(100, Decimal("0.5")) + Decimal("1.00"))


def test_maker_path_uses_the_flat_fee():
    f = KalshiFees(maker_fee_per_contract=Decimal("0.0025"))
    assert f.trade_fee(100, Decimal("0.50"), maker=True) == Decimal("0.25")
    assert f.trade_fee(100, Decimal("0.50"), maker=False) == Decimal("1.75")


def test_polymarket_defaults_to_free_but_charges_configured_costs():
    assert PolymarketFees().trade_fee(100, Decimal("0.5")) == ZERO
    gas = PolymarketFees(fixed_cost_per_trade=Decimal("0.75"))
    assert gas.trade_fee(100, Decimal("0.5")) == Decimal("0.75")
    prop = PolymarketFees(proportional_fee=Decimal("0.02"))
    assert prop.trade_fee(100, Decimal("0.5")) == Decimal("1.00")
