"""
The verification interview.

The property under test throughout: the careless path must be harder than the
careful one. Nothing verifies except by an explicit affirmative on every
divergence class.
"""
import pytest

from pairing import MarketPair, ResolutionTerms, RiskFlag, Verification
from review import QUESTIONS, render_pair, run_review


def pair(tokens=True) -> MarketPair:
    return MarketPair(
        pair_id="fed-dec",
        kalshi=ResolutionTerms("kalshi", "FED-25DEC", "Fed cuts in Dec",
                               resolution_source="FOMC", close_time="2025-12-31T23:59:00Z",
                               rules_excerpt="Resolves per the FOMC statement."),
        polymarket=ResolutionTerms("polymarket", "0xabc", "Fed cuts in Dec",
                                   resolution_source="FOMC",
                                   rules_excerpt="Resolves YES if lowered."),
        polymarket_yes_token="tokYES" if tokens else "",
        polymarket_no_token="tokNO" if tokens else "",
    )


def scripted(answers):
    """A reader that returns queued answers, then raises if over-consumed."""
    queue = list(answers)

    def reader(_prompt):
        if not queue:
            raise AssertionError("asked more questions than expected")
        return queue.pop(0)
    return reader


def silent(_msg):
    pass


ALL_SAME = ["same"] * (len(QUESTIONS) + 1)


# ----------------------------------------------------------------------
# The happy path requires every answer
# ----------------------------------------------------------------------

def test_all_same_verifies():
    p = pair()
    out = run_review(p, "tim", reader=scripted(ALL_SAME), writer=silent)
    assert out.verified and p.is_tradeable
    assert p.verified_by == "tim"
    assert p.verified_at is not None


def test_every_divergence_class_is_asked():
    asked = []

    def reader(_):
        asked.append(1)
        return "same"

    run_review(pair(), "tim", reader=reader, writer=silent)
    assert len(asked) == len(QUESTIONS) + 1, "all blocking classes plus settlement"


# ----------------------------------------------------------------------
# Blocking answers
# ----------------------------------------------------------------------

@pytest.mark.parametrize("index", range(len(QUESTIONS)))
def test_differ_on_any_blocking_class_rejects(index):
    answers = ["same"] * index + ["differ"]
    p = pair()
    out = run_review(p, "tim", reader=scripted(answers), writer=silent)
    assert out.rejected and not out.verified
    assert p.verification is Verification.REJECTED
    assert not p.is_tradeable
    assert QUESTIONS[index].flag in p.risk_flags


def test_rejection_stops_asking_further_questions():
    """No path exists where a known divergence is recorded and still verified."""
    answers = ["differ"]          # scripted() raises if asked again
    run_review(pair(), "tim", reader=scripted(answers), writer=silent)


# ----------------------------------------------------------------------
# The default is the unsafe answer, and it blocks
# ----------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["", "y", "yes", "ok", "sure", "n", "  ", "SAME "])
def test_only_the_exact_word_same_advances(answer):
    """
    Pressing return six times must not verify a pair. Anything unrecognised
    aborts without recording a decision.
    """
    if answer.strip().lower() == "same":
        pytest.skip("this one is the affirmative")
    p = pair()
    out = run_review(p, "tim", reader=scripted([answer]), writer=silent)
    assert out.aborted
    assert not out.verified and not out.rejected
    assert p.verification is Verification.UNVERIFIED, "an abort records nothing"


def test_abort_midway_leaves_pair_untouched():
    p = pair()
    run_review(p, "tim", reader=scripted(["same", "same", "quit"]), writer=silent)
    assert p.verification is Verification.UNVERIFIED
    assert p.risk_flags == []
    assert p.verified_at is None


# ----------------------------------------------------------------------
# Settlement lag is recorded but does not block
# ----------------------------------------------------------------------

def test_settlement_lag_records_without_blocking():
    p = pair()
    out = run_review(p, "tim",
                     reader=scripted(["same"] * len(QUESTIONS) + ["differ"]),
                     writer=silent)
    assert out.verified and p.is_tradeable
    assert RiskFlag.SETTLEMENT_LAG in p.risk_flags


# ----------------------------------------------------------------------
# Unscannable pairs are refused before the interview starts
# ----------------------------------------------------------------------

def test_pair_without_tokens_is_refused_immediately():
    """Verifying a pair that can never be scanned wastes the reviewer's time."""
    p = pair(tokens=False)
    out = run_review(p, "tim", reader=scripted([]), writer=silent)
    assert out.aborted
    assert "token" in out.reason
    assert p.verification is Verification.UNVERIFIED


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def test_render_shows_both_sides_and_tokens():
    text = render_pair(pair())
    for expected in ("FED-25DEC", "0xabc", "FOMC", "tokYES", "tokNO",
                     "Resolves per the FOMC statement.", "Resolves YES if lowered."):
        assert expected in text


def test_render_flags_missing_tokens_loudly():
    assert "MISSING" in render_pair(pair(tokens=False))
