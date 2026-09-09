"""Cross-venue numeric domain conversions."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pytest

from money import (
    MAX_PROB, MIN_PROB, ZERO, MoneyError,
    cents_to_prob, centi_to_prob, clamp_prob, fmt_cents, fmt_usd,
    prob_to_cents, prob_to_centi, round_up_cent, to_decimal,
)


@pytest.mark.parametrize("cents,prob", [(1, "0.01"), (37, "0.37"), (50, "0.50"), (99, "0.99")])
def test_kalshi_cents_convert_exactly(cents, prob):
    assert cents_to_prob(cents) == Decimal(prob)
    assert prob_to_cents(Decimal(prob)) == cents


def test_float_ingest_uses_shortest_repr():
    assert to_decimal(0.37) == Decimal("0.37")


def test_bool_is_rejected_as_a_quantity():
    """bool is an int subclass; True silently becoming 1c would be a real bug."""
    with pytest.raises(MoneyError):
        to_decimal(True)


def test_non_finite_rejected():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(MoneyError):
            to_decimal(bad)


def test_prices_clamp_into_open_interval():
    assert clamp_prob(0) == MIN_PROB
    assert clamp_prob(1) == MAX_PROB
    assert clamp_prob("-3") == MIN_PROB


def test_directional_cent_rounding_never_improves_the_price():
    """
    Kalshi accepts whole cents only. Rounding a buy down would quote a price
    the venue will not fill at the edge you computed.
    """
    p = Decimal("0.375")
    assert prob_to_cents(p, rounding=ROUND_CEILING) == 38   # buying
    assert prob_to_cents(p, rounding=ROUND_FLOOR) == 37     # selling


def test_centi_roundtrip_exact():
    for v in ("0.0001", "0.3333", "0.9999", "0.5"):
        assert centi_to_prob(prob_to_centi(Decimal(v))) == Decimal(v)


def test_fee_rounding_goes_up_to_the_cent():
    assert round_up_cent("1.7401") == Decimal("1.75")
    assert round_up_cent("1.7500") == Decimal("1.75")
    assert round_up_cent("0.0001") == Decimal("0.01")


def test_display_helpers():
    assert fmt_usd("1234.5") == "$1,234.50"
    assert fmt_cents("0.37") == "37c"
