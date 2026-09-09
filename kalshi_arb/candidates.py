"""
Candidate pair suggestion.

Strictly a triage aid. This narrows thousands of market combinations down to a
few dozen worth a human reading two rulebooks; it never decides that two
markets are equivalent. Everything it emits lands as UNVERIFIED, and
pairing.py is what refuses to trade those.

The distinction matters because title similarity is actively misleading here.
"Fed cuts rates in December" and "Fed cuts rates in December" can be a perfect
string match and still resolve differently — on the meeting date versus the
effective date, on the target range versus the effective rate, on what happens
in an intermeeting cut. Similarity finds candidates; only the rulebooks decide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from pairing import MarketPair, ResolutionTerms

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "be", "is", "are", "to", "of", "in", "on", "at",
    "by", "for", "and", "or", "this", "that", "it", "as", "before", "after",
    "than", "more", "less", "any", "market", "question",
})

_TOKEN_RE = re.compile(r"[a-z0-9.%$]+")


def normalise(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS]


def similarity(a: str, b: str) -> float:
    """
    Blend token overlap with sequence similarity, in [0, 1].

    Jaccard alone rewards two markets that share a topic but ask opposite
    questions; sequence ratio alone rewards boilerplate phrasing. Neither is
    reliable, which is the point — this is a ranking heuristic, not a decision.
    """
    ta, tb = set(normalise(a)), set(normalise(b))
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(0.6 * jaccard + 0.4 * ratio, 4)


@dataclass(frozen=True)
class Candidate:
    score: float
    kalshi_ticker: str
    kalshi_title: str
    polymarket_id: str
    polymarket_title: str

    def to_pair(self) -> MarketPair:
        """Build an UNVERIFIED pair for a human to review."""
        return MarketPair(
            pair_id=f"{self.kalshi_ticker}__{self.polymarket_id[:12]}",
            kalshi=ResolutionTerms("kalshi", self.kalshi_ticker, self.kalshi_title),
            polymarket=ResolutionTerms("polymarket", self.polymarket_id, self.polymarket_title),
            notes=(
                f"Auto-suggested at similarity {self.score}. NOT verified — read "
                "both rulebooks and confirm resolution source, observation time, "
                "rounding, revision handling and edge-case wording before trading."
            ),
        )


def suggest(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
    *,
    threshold: float = 0.45,
    limit: int = 50,
) -> list[Candidate]:
    """Rank cross-venue title matches above `threshold`, best first."""
    out: list[Candidate] = []
    for km in kalshi_markets:
        k_title = km.get("title") or km.get("subtitle") or ""
        k_ticker = km.get("ticker", "")
        if not k_title or not k_ticker:
            continue
        for pm in polymarket_markets:
            p_title = pm.get("question") or pm.get("title") or ""
            p_id = pm.get("conditionId") or pm.get("condition_id") or pm.get("id") or ""
            if not p_title or not p_id:
                continue
            score = similarity(k_title, p_title)
            if score >= threshold:
                out.append(Candidate(score, k_ticker, k_title, str(p_id), p_title))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]
