"""Spread detection: edge is real, sized to depth, and fee-inclusive."""
from decimal import Decimal

import pytest

from fees import KalshiFees, PolymarketFees
from money import ZERO
from orderbook import Book, Level, book_from_kalshi, book_from_polymarket
from pairing import MarketPair, ResolutionTerms, RiskFlag, Verification
from scanner import price_direction, scan_pair

NO_FEES = (KalshiFees(taker_coefficient=ZERO), PolymarketFees())


def make_pair(verified=True, close_time=None) -> MarketPair:
    p = MarketPair(
        pair_id="test-pair",
        kalshi=ResolutionTerms("kalshi", "TEST", "Test", close_time=close_time),
        polymarket=ResolutionTerms("polymarket", "0x1", "Test"),
    )
    if verified:
        p.mark_verified("test")
    return p


def book(venue, outcome, asks, bids=()) -> Book:
    return Book(
        venue=venue, market_id="m", outcome=outcome,
        asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
    )


# ----------------------------------------------------------------------
# Core edge detection
# ----------------------------------------------------------------------

def test_detects_edge_when_prices_sum_below_one():
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s is not None
    assert s.contracts == 100
    assert s.gross_edge_per_contract == Decimal("0.0500")
    assert s.net_profit == Decimal("5.0000")
    assert s.capital_required == Decimal("95.0000")


def test_no_edge_when_prices_sum_above_one():
    k, p = NO_FEES
    assert price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.60", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    ) is None


def test_exactly_one_dollar_is_not_an_edge():
    k, p = NO_FEES
    assert price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.45", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    ) is None


def test_empty_book_yields_nothing():
    k, p = NO_FEES
    assert price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", []),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    ) is None


# ----------------------------------------------------------------------
# The failure this scanner is built to avoid: quoting top-of-book edge
# ----------------------------------------------------------------------

def test_size_is_limited_by_the_thinner_leg():
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 500)]),
        poly_book=book("polymarket", "NO", [("0.55", 20)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.contracts == 20


def test_walks_into_worse_levels_only_while_profitable():
    """
    Level 1 is profitable, level 2 is not. A top-of-book scanner would report
    600 contracts of edge; only 100 actually clear.
    """
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100), ("0.60", 500)]),
        poly_book=book("polymarket", "NO", [("0.55", 600)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.contracts == 100
    assert s.kalshi_price == Decimal("0.4000")


def test_vwap_reflects_multiple_levels_consumed():
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100), ("0.42", 100)]),
        poly_book=book("polymarket", "NO", [("0.50", 200)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.contracts == 200
    assert s.kalshi_price == Decimal("0.4100")   # (100*.40 + 100*.42)/200


def test_reported_profit_is_achievable_at_reported_size():
    """The invariant that matters: payout - capital == net_profit, exactly."""
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.30", 37), ("0.33", 63)]),
        poly_book=book("polymarket", "NO", [("0.60", 40), ("0.62", 60)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.net_profit == Decimal(s.contracts) - s.capital_required


# ----------------------------------------------------------------------
# Fees
# ----------------------------------------------------------------------

def test_fees_can_erase_a_gross_edge():
    """A 1-cent gross edge at 50c does not survive Kalshi's 1.75c fee."""
    thin = dict(
        kalshi_book=book("kalshi", "YES", [("0.495", 100)]),
        poly_book=book("polymarket", "NO", [("0.495", 100)]),
    )
    k_free, p_free = NO_FEES
    assert price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO", **thin,
        kalshi_fees=k_free, poly_fees=p_free,
    ) is not None
    assert price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO", **thin,
        kalshi_fees=KalshiFees(), poly_fees=PolymarketFees(),
    ) is None


def test_wide_edge_survives_fees_and_capital_includes_them():
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.30", 100)]),
        poly_book=book("polymarket", "NO", [("0.60", 100)]),
        kalshi_fees=KalshiFees(), poly_fees=PolymarketFees(),
    )
    assert s is not None
    assert s.fees_total > ZERO
    assert s.capital_required > Decimal("90")
    assert s.net_profit == Decimal(100) - s.capital_required


def test_polymarket_fixed_cost_is_charged():
    base = dict(
        kalshi_book=book("kalshi", "YES", [("0.30", 100)]),
        poly_book=book("polymarket", "NO", [("0.60", 100)]),
        kalshi_fees=KalshiFees(taker_coefficient=ZERO),
    )
    free = price_direction(make_pair(), direction="KALSHI_YES_POLY_NO",
                           poly_fees=PolymarketFees(), **base)
    gassy = price_direction(make_pair(), direction="KALSHI_YES_POLY_NO",
                            poly_fees=PolymarketFees(fixed_cost_per_trade=Decimal("2.50")), **base)
    assert gassy.net_profit == free.net_profit - Decimal("2.50")


# ----------------------------------------------------------------------
# Resolution gating
# ----------------------------------------------------------------------

def test_unverified_pair_is_priced_but_not_tradeable():
    k, p = NO_FEES
    s = price_direction(
        make_pair(verified=False), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s is not None, "research value: you want to see the edge exists"
    assert s.tradeable is False
    assert "verified" in s.block_reason


def test_blocking_flag_makes_pair_untradeable_however_wide_the_edge():
    pair = make_pair()
    pair.risk_flags.append(RiskFlag.SOURCE_MISMATCH)
    k, p = NO_FEES
    s = price_direction(
        pair, direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.10", 100)]),
        poly_book=book("polymarket", "NO", [("0.10", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.net_profit > Decimal("70")
    assert s.tradeable is False
    assert "SOURCE_MISMATCH" in s.block_reason


# ----------------------------------------------------------------------
# Return metrics
# ----------------------------------------------------------------------

def test_roi_and_annualisation():
    k, p = NO_FEES
    from datetime import datetime, timedelta, timezone
    close = (datetime.now(timezone.utc) + timedelta(days=36.5)).isoformat()
    s = price_direction(
        make_pair(close_time=close), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.roi == pytest.approx(Decimal("0.052631"), abs=Decimal("0.00001"))
    # ~5.26% over ~36.5 days annualises to roughly 10x that.
    assert s.annualised_roi > Decimal("0.5")


def test_annualisation_is_none_without_a_close_time():
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=book("kalshi", "YES", [("0.40", 100)]),
        poly_book=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.annualised_roi is None


# ----------------------------------------------------------------------
# Both directions
# ----------------------------------------------------------------------

def test_scan_pair_prices_both_directions_best_first():
    k, p = NO_FEES
    spreads = scan_pair(
        make_pair(),
        kalshi_yes=book("kalshi", "YES", [("0.40", 100)]),
        kalshi_no=book("kalshi", "NO", [("0.30", 100)]),
        poly_yes=book("polymarket", "YES", [("0.50", 100)]),
        poly_no=book("polymarket", "NO", [("0.55", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert len(spreads) == 2
    assert spreads[0].net_profit >= spreads[1].net_profit
    assert spreads[0].direction == "POLY_YES_KALSHI_NO"   # 0.50 + 0.30 = 0.80


def test_kalshi_book_inversion_feeds_the_scanner_correctly():
    """
    Kalshi has no ask book. A NO bid at 58c IS the YES offer at 42c; getting
    this backwards prices every spread against the wrong side.
    """
    yes = book_from_kalshi("T", "YES", yes_levels=[(40, 100)], no_levels=[(58, 100)])
    assert yes.best_ask == Decimal("0.4200")
    k, p = NO_FEES
    s = price_direction(
        make_pair(), direction="KALSHI_YES_POLY_NO",
        kalshi_book=yes,
        poly_book=book_from_polymarket("t", "NO", bids=[], asks=[("0.50", 100)]),
        kalshi_fees=k, poly_fees=p,
    )
    assert s.kalshi_price == Decimal("0.4200")
    assert s.gross_edge_per_contract == Decimal("0.0800")
