"""Exactness tests for the fixed-point money layer."""
from decimal import Decimal

import pytest

from money import (
    ZERO, MIN_PRICE, MAX_PRICE,
    to_decimal, quantize_usdc, quantize_price, clamp_price,
    shares_for_cost, notional,
    usdc_to_micro, micro_to_usdc, price_to_centi, centi_to_price,
    shares_to_micro, micro_to_shares, MoneyError,
)


def test_float_ingest_uses_shortest_repr_not_binary_expansion():
    # Decimal(0.1) would give 0.1000000000000000055511151231257827...
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal("0.1") == Decimal("0.1")
    assert to_decimal(3) == Decimal("3")


def test_rejects_non_finite_and_junk():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(MoneyError):
            to_decimal(bad)
    with pytest.raises(MoneyError):
        to_decimal("not-a-number")
    with pytest.raises(MoneyError):
        to_decimal(None)


def test_price_clamped_into_open_interval():
    assert clamp_price(0) == MIN_PRICE
    assert clamp_price(1) == MAX_PRICE
    assert clamp_price(-5) == MIN_PRICE
    assert clamp_price(2) == MAX_PRICE
    assert clamp_price("0.5000") == Decimal("0.5")


def test_valuation_is_deterministic_and_symmetric():
    """
    The same (shares, price) pair must always value identically. Asymmetric
    rounding here would manufacture PnL on a flat round-trip.
    """
    shares, price = Decimal("3.333333"), Decimal("0.3333")
    assert notional(shares, price) == notional(shares, price)
    assert notional(shares, price) - notional(shares, price) == ZERO
    # Within half a quantum of the exact product.
    assert abs(notional(shares, price) - shares * price) <= Decimal("0.0000005")


def test_shares_for_cost_never_exceeds_budget():
    for budget in ("10", "33.33", "0.07", "1234.56"):
        for px in ("0.01", "0.37", "0.9999", "0.0001"):
            shares = shares_for_cost(budget, px)
            assert notional(shares, px) <= quantize_usdc(budget) + Decimal("0.000001")


def test_zero_and_negative_sizes_are_inert():
    assert shares_for_cost(0, "0.5") == ZERO
    assert shares_for_cost("-5", "0.5") == ZERO
    assert notional(0, "0.5") == ZERO
    assert notional("-1", "0.5") == ZERO


def test_zero_price_does_not_raise():
    """Division by price must be total — clamping is what makes it so."""
    assert shares_for_cost("10", 0) > ZERO
    assert shares_for_cost("10", "0.0") > ZERO


@pytest.mark.parametrize("value", ["0", "0.000001", "1234567.891234", "-42.5"])
def test_usdc_roundtrip_is_exact(value):
    d = Decimal(value)
    assert micro_to_usdc(usdc_to_micro(d)) == d


@pytest.mark.parametrize("value", ["0.0001", "0.5", "0.9999", "0.3333"])
def test_price_roundtrip_is_exact(value):
    d = Decimal(value)
    assert centi_to_price(price_to_centi(d)) == d


def test_share_roundtrip_is_exact():
    d = Decimal("3.333333")
    assert micro_to_shares(shares_to_micro(d)) == d


def test_thousand_roundtrips_do_not_drift():
    """
    The failure mode this module exists to prevent: repeated encode/decode
    through storage accumulating error. With REAL columns this drifts; with
    integer minor units it cannot.
    """
    v = Decimal("33.333333")
    for _ in range(1000):
        v = micro_to_usdc(usdc_to_micro(v))
    assert v == Decimal("33.333333")
