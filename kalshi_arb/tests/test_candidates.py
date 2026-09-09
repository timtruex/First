"""Candidate suggestion is triage only, never a decision."""
from candidates import Candidate, similarity, suggest
from pairing import Verification


def test_identical_questions_score_high():
    assert similarity("Will the Fed cut rates in December?", "Fed cuts rates in December") > 0.6


def test_unrelated_questions_score_low():
    assert similarity("Will the Fed cut rates in December?", "Will it snow in NYC?") < 0.3


def test_opposite_questions_still_score_high():
    """
    The documented trap: 'cut' vs 'raise' are one token apart and score well.
    This is precisely why suggestions can never auto-verify.
    """
    score = similarity(
        "Will the Fed cut rates in December?",
        "Will the Fed raise rates in December?",
    )
    assert score > 0.5


def test_empty_titles_score_zero():
    assert similarity("", "anything") == 0.0
    assert similarity("Will the Fed cut?", "") == 0.0


def test_suggestions_are_always_unverified():
    c = Candidate(0.99, "K1", "Fed cuts", "0xabc", "Fed cuts")
    p = c.to_pair()
    assert p.verification is Verification.UNVERIFIED
    assert not p.is_tradeable
    assert "NOT verified" in p.notes


def test_suggest_respects_threshold_and_orders_best_first():
    k = [{"ticker": "K1", "title": "Will the Fed cut rates in December?"}]
    p = [
        {"conditionId": "0xa", "question": "Fed cuts rates in December"},
        {"conditionId": "0xb", "question": "Will it snow in Denver?"},
    ]
    got = suggest(k, p, threshold=0.4)
    assert [c.polymarket_id for c in got] == ["0xa"]


def test_suggest_skips_records_missing_ids_or_titles():
    k = [{"ticker": "", "title": "Fed cuts"}, {"ticker": "K1", "title": ""}]
    p = [{"conditionId": "0xa", "question": "Fed cuts"}]
    assert suggest(k, p, threshold=0.0) == []
