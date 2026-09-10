"""
Market payload normalisation.

The token mapping tests are the important ones: an inverted YES/NO mapping
does not error, it prices the opposite book and reports a spread that is the
exact reverse of reality.
"""
import pytest

from metadata import (
    MetadataError, enrich_pair, kalshi_terms, polymarket_terms, polymarket_tokens,
)
from pairing import MarketPair, ResolutionTerms


def poly(**kw):
    base = {
        "conditionId": "0xabc123456789",
        "question": "Fed cuts rates in December",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["tokYES","tokNO"]',
        "endDate": "2025-12-31T23:59:00Z",
        "resolutionSource": "FOMC statement",
        "slug": "fed-dec",
        "description": "Resolves YES if the target range is lowered.",
    }
    base.update(kw)
    return base


# ----------------------------------------------------------------------
# Token mapping — by label, never by position
# ----------------------------------------------------------------------

def test_maps_tokens_by_label():
    assert polymarket_tokens(poly()) == ("tokYES", "tokNO")


def test_reversed_outcome_order_maps_correctly():
    """
    The trap. Assuming index 0 is YES silently inverts the pair, and an
    inverted pair reports the exact opposite of the real spread.
    """
    m = poly(outcomes='["No","Yes"]', clobTokenIds='["tokNO","tokYES"]')
    assert polymarket_tokens(m) == ("tokYES", "tokNO")


def test_case_and_whitespace_tolerant():
    m = poly(outcomes='[" YES ","no"]')
    assert polymarket_tokens(m) == ("tokYES", "tokNO")


def test_accepts_real_lists_not_only_json_strings():
    m = poly(outcomes=["Yes", "No"], clobTokenIds=["a", "b"])
    assert polymarket_tokens(m) == ("a", "b")


def test_non_binary_market_is_refused_not_guessed():
    m = poly(outcomes='["Trump","Biden"]', clobTokenIds='["a","b"]')
    with pytest.raises(MetadataError, match="not a Yes/No pair"):
        polymarket_tokens(m)


def test_multi_outcome_market_refused():
    m = poly(outcomes='["A","B","C"]', clobTokenIds='["a","b","c"]')
    with pytest.raises(MetadataError, match="binary"):
        polymarket_tokens(m)


def test_count_mismatch_refused():
    m = poly(outcomes='["Yes","No"]', clobTokenIds='["only-one"]')
    with pytest.raises(MetadataError, match="mismatch"):
        polymarket_tokens(m)


def test_missing_tokens_refused():
    m = poly(clobTokenIds=None)
    with pytest.raises(MetadataError, match="no clobTokenIds"):
        polymarket_tokens(m)


def test_malformed_json_refused():
    m = poly(clobTokenIds='[not json')
    with pytest.raises(MetadataError, match="not valid JSON"):
        polymarket_tokens(m)


# ----------------------------------------------------------------------
# Terms extraction
# ----------------------------------------------------------------------

def test_polymarket_terms_populated():
    t = polymarket_terms(poly())
    assert t.market_id == "0xabc123456789"
    assert t.resolution_source == "FOMC statement"
    assert t.close_time == "2025-12-31T23:59:00Z"
    assert "polymarket.com/event/fed-dec" in t.rules_url
    assert "target range" in t.rules_excerpt


def test_kalshi_terms_join_both_rule_sections():
    t = kalshi_terms({
        "ticker": "FED-25DEC", "title": "Fed cuts in December",
        "close_time": "2025-12-31T23:59:00Z",
        "rules_primary": "Primary rule text.", "rules_secondary": "Secondary rule text.",
    })
    assert t.market_id == "FED-25DEC"
    assert "Primary rule text." in t.rules_excerpt
    assert "Secondary rule text." in t.rules_excerpt
    assert t.close_time == "2025-12-31T23:59:00Z"


def test_kalshi_without_ticker_refused():
    with pytest.raises(MetadataError, match="no ticker"):
        kalshi_terms({"title": "x"})


def test_long_rules_are_truncated_with_a_marker():
    t = kalshi_terms({"ticker": "T", "rules_primary": "x" * 5000})
    assert len(t.rules_excerpt) < 1300
    assert t.rules_excerpt.endswith("[…]")


# ----------------------------------------------------------------------
# enrich_pair collects problems instead of raising
# ----------------------------------------------------------------------

def test_enrich_populates_tokens_and_terms():
    pair = MarketPair("p", ResolutionTerms("kalshi", "T", "t"),
                      ResolutionTerms("polymarket", "0x", "p"))
    problems = enrich_pair(pair, {"ticker": "FED-25DEC", "title": "Fed"}, poly())
    assert problems == []
    assert pair.polymarket_yes_token == "tokYES"
    assert pair.polymarket_no_token == "tokNO"
    assert pair.kalshi.market_id == "FED-25DEC"


def test_enrich_reports_problems_without_raising():
    """One malformed market must not abort a refresh over the whole registry."""
    pair = MarketPair("p", ResolutionTerms("kalshi", "T", "t"),
                      ResolutionTerms("polymarket", "0x", "p"))
    problems = enrich_pair(pair, {"ticker": "T"}, poly(outcomes='["A","B"]'))
    assert any("tokens" in p for p in problems)
    assert pair.polymarket_yes_token == ""


def test_enrich_tolerates_missing_payloads():
    pair = MarketPair("p", ResolutionTerms("kalshi", "T", "t"),
                      ResolutionTerms("polymarket", "0x", "p"))
    assert enrich_pair(pair, None, None) == []
