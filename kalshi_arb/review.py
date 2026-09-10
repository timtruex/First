"""
Interactive resolution-equivalence review.

This is the step the whole strategy rests on, so the interface is built to
make the careless path harder than the careful one:

  - Every divergence class is asked explicitly. A reviewer who has not
    considered revision handling has to actively answer a question about it
    rather than skip a field they did not know existed.
  - The default answer to every question is the unsafe one ("I don't know"),
    which blocks. Verification requires typing an affirmative for each class,
    not pressing return six times.
  - Answering "they differ" on any blocking class rejects the pair
    immediately and stops asking. There is no path where a reviewer records a
    known divergence and still verifies.
  - The reviewer's name and the timestamp are recorded, because "who checked
    this and when" is the first question after a pair resolves badly.

The prompts are deliberately specific. "Do the rules match?" invites a yes;
"Does Kalshi settle on the first print while Polymarket settles on the
revised figure?" invites a reader to go and look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pairing import BLOCKING_FLAGS, MarketPair, RiskFlag


@dataclass(frozen=True)
class Question:
    flag: RiskFlag
    prompt: str
    detail: str


# Ordered worst-first: the classes most likely to differ and most costly when
# they do are asked while the reviewer is still paying attention.
QUESTIONS: tuple[Question, ...] = (
    Question(
        RiskFlag.SOURCE_MISMATCH,
        "Do BOTH markets resolve from the same underlying source?",
        "e.g. one says 'per the Associated Press', the other 'per the official "
        "state canvass'. Different sources can disagree for days, or forever.",
    ),
    Question(
        RiskFlag.TIMING_MISMATCH,
        "Do both observe the outcome at the same moment?",
        "e.g. close of business vs midnight UTC, or the meeting date vs the "
        "effective date. A market can be YES on one venue and NO on the other "
        "for a whole day.",
    ),
    Question(
        RiskFlag.ROUNDING_MISMATCH,
        "Do both round the underlying figure identically?",
        "e.g. CPI quoted to one decimal vs as-published. A print of 3.049% is "
        "'above 3.0' on one venue and not on the other.",
    ),
    Question(
        RiskFlag.REVISION_HANDLING,
        "Do both use the same print — first release or revised?",
        "Economic data is revised. If one settles on the initial print and the "
        "other on the revision, they can resolve opposite ways.",
    ),
    Question(
        RiskFlag.EDGE_CASE_WORDING,
        "Do both handle the edge cases the same way?",
        "Postponement, cancellation, a candidate withdrawing, a series ending "
        "early, ties. This is where near-identical questions most often part.",
    ),
)

# Recorded but non-blocking: a funding cost, not a divergence in payout.
SETTLEMENT_QUESTION = Question(
    RiskFlag.SETTLEMENT_LAG,
    "Do both free your capital at roughly the same time?",
    "Not a payout risk, but capital locked days longer on one venue is a real "
    "cost. Answering 'no' records the flag without blocking the pair.",
)


def render_pair(pair: MarketPair) -> str:
    """Side-by-side terms, so review does not require two browser tabs."""
    k, p = pair.kalshi, pair.polymarket
    lines = [
        "=" * 78,
        f"PAIR  {pair.pair_id}",
        "=" * 78,
        "",
        "KALSHI",
        f"  market      {k.market_id}",
        f"  title       {k.title}",
        f"  source      {k.resolution_source or '(not published)'}",
        f"  closes      {k.close_time or '(unknown)'}",
        f"  settles     {k.expected_settlement or '(unknown)'}",
        f"  url         {k.rules_url or '(none)'}",
        "",
        "POLYMARKET",
        f"  market      {p.market_id}",
        f"  title       {p.title}",
        f"  source      {p.resolution_source or '(not published)'}",
        f"  closes      {p.close_time or '(unknown)'}",
        f"  url         {p.rules_url or '(none)'}",
        f"  yes token   {pair.polymarket_yes_token or '(MISSING — pair is unscannable)'}",
        f"  no  token   {pair.polymarket_no_token or '(MISSING — pair is unscannable)'}",
        "",
    ]
    if k.rules_excerpt:
        lines += ["KALSHI RULES", *(f"  {ln}" for ln in k.rules_excerpt.splitlines()), ""]
    if p.rules_excerpt:
        lines += ["POLYMARKET RULES", *(f"  {ln}" for ln in p.rules_excerpt.splitlines()), ""]
    return "\n".join(lines)


def _ask(q: Question, reader: Callable[[str], str], writer: Callable[[str], None]) -> str:
    writer("")
    writer(f"[{q.flag.value}]")
    writer(f"  {q.detail}")
    # "same" must be typed. Return, "y", and anything unrecognised all fall
    # through to the blocking answer.
    writer(f"  {q.prompt}")
    writer("  Type 'same' if you have checked and they match, "
           "'differ' if they do not, anything else to abort: ")
    return reader("").strip().lower()


@dataclass
class ReviewOutcome:
    verified: bool
    rejected: bool
    aborted: bool
    flags: list[RiskFlag]
    reason: str = ""


def run_review(
    pair: MarketPair,
    reviewer: str,
    *,
    reader: Callable[[str], str] | None = None,
    writer: Callable[[str], None] | None = None,
) -> ReviewOutcome:
    """
    Walk the divergence classes. Mutates `pair` to record the outcome.

    Any answer other than an explicit "same" on a blocking class stops the
    review — "differ" rejects the pair, anything else aborts without recording
    a decision at all, so an interrupted review never leaves a pair looking
    reviewed.
    """
    # Resolved here rather than as default arguments: a default of `input`
    # binds the builtin at definition time, which makes the prompt loop
    # impossible to drive from a test.
    reader = reader if reader is not None else input
    writer = writer if writer is not None else print

    writer(render_pair(pair))

    if not (pair.polymarket_yes_token and pair.polymarket_no_token):
        writer("REFUSING: this pair has no Polymarket token ids, so it cannot be")
        writer("scanned even if verified. Run 'refresh' first.")
        return ReviewOutcome(False, False, True, [], "missing token ids")

    writer("Answer from the rulebooks, not from the titles. Six questions.")

    for q in QUESTIONS:
        answer = _ask(q, reader, writer)
        if answer == "differ":
            pair.mark_rejected(reviewer, q.flag,
                               notes=f"Reviewer found divergence: {q.flag.value}")
            writer("")
            writer(f"REJECTED on {q.flag.value}. This pair will never be traded.")
            return ReviewOutcome(False, True, False, [q.flag], q.flag.value)
        if answer != "same":
            writer("")
            writer("Aborted — no decision recorded. The pair stays UNVERIFIED.")
            return ReviewOutcome(False, False, True, [], "aborted by reviewer")

    # Non-blocking: recorded either way.
    flags: list[RiskFlag] = []
    answer = _ask(SETTLEMENT_QUESTION, reader, writer)
    if answer == "differ":
        flags.append(RiskFlag.SETTLEMENT_LAG)
        if RiskFlag.SETTLEMENT_LAG not in pair.risk_flags:
            pair.risk_flags.append(RiskFlag.SETTLEMENT_LAG)
        writer("  Recorded SETTLEMENT_LAG (does not block trading).")
    elif answer != "same":
        writer("")
        writer("Aborted — no decision recorded. The pair stays UNVERIFIED.")
        return ReviewOutcome(False, False, True, [], "aborted by reviewer")

    pair.mark_verified(reviewer, notes=f"Reviewed by {reviewer}; all divergence classes checked.")
    writer("")
    writer(f"VERIFIED by {reviewer}. This pair is now tradeable.")
    if flags:
        writer(f"Recorded non-blocking flags: {', '.join(f.value for f in flags)}")
    return ReviewOutcome(True, False, False, flags)
