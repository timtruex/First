"""
Cross-venue market pairing and resolution-equivalence gating.

This is the module that decides whether the strategy is arbitrage or a
disguised directional bet, so it is worth being explicit about the failure it
exists to prevent.

A cross-venue "arb" buys YES on one venue and NO on the other. If both markets
resolve identically, exactly one leg pays $1 and the position is riskless: you
capture (1 - total cost) regardless of the outcome. If they resolve
*differently*, both legs lose and you lose the entire capital deployed — not
the edge, the capital. A 2-cent edge against a 98-cent downside needs the
resolution criteria to match on well over 99% of pairs just to break even.

Two markets can look identical and resolve differently for mundane reasons:

  - different data source        ("per AP" vs "per the official state canvass")
  - different observation time   (close of business vs midnight UTC)
  - different rounding           (CPI to one decimal vs as-published)
  - different revision handling  (first print vs revised figure)
  - different edge-case wording  (what happens if the event is postponed,
                                  a candidate withdraws, a series ends early)
  - different settlement lag     (capital freed days apart, which is a funding
                                  cost even when both resolve the same way)

None of these are detectable by fuzzy-matching market titles, which is why
this module does not attempt it. Pairs are curated and must carry an explicit
human verification before the scanner will call a spread actionable. Title
similarity is used only to *suggest* candidates for review.

The scanner will still price unverified pairs — you want to see whether an
edge exists before spending time reading two rulebooks — but it labels them
RESEARCH and refuses to mark them tradeable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator


class Verification(str, Enum):
    """Whether a human has confirmed the two markets resolve identically."""

    UNVERIFIED = "UNVERIFIED"   # nobody has read both rulebooks
    VERIFIED = "VERIFIED"       # confirmed equivalent; tradeable
    REJECTED = "REJECTED"       # confirmed NOT equivalent; never trade
    EXPIRED = "EXPIRED"         # was verified, but a rulebook changed since


class RiskFlag(str, Enum):
    """Specific, named ways a pair can diverge. Each must be ruled out."""

    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    TIMING_MISMATCH = "TIMING_MISMATCH"
    ROUNDING_MISMATCH = "ROUNDING_MISMATCH"
    REVISION_HANDLING = "REVISION_HANDLING"
    EDGE_CASE_WORDING = "EDGE_CASE_WORDING"
    SETTLEMENT_LAG = "SETTLEMENT_LAG"
    LIQUIDITY_ASYMMETRY = "LIQUIDITY_ASYMMETRY"


# Flags that make a pair untradeable no matter how wide the spread: these are
# the ones where the two contracts can pay out differently.
BLOCKING_FLAGS: frozenset[RiskFlag] = frozenset({
    RiskFlag.SOURCE_MISMATCH,
    RiskFlag.TIMING_MISMATCH,
    RiskFlag.ROUNDING_MISMATCH,
    RiskFlag.REVISION_HANDLING,
    RiskFlag.EDGE_CASE_WORDING,
})


@dataclass
class ResolutionTerms:
    """How one venue's market decides its outcome."""

    venue: str
    market_id: str
    title: str
    resolution_source: str = ""
    close_time: str | None = None        # ISO-8601 UTC
    expected_settlement: str | None = None
    rules_url: str = ""
    rules_excerpt: str = ""


@dataclass
class MarketPair:
    """
    One Kalshi market paired with one Polymarket market.

    `kalshi_yes` + `polymarket_no` is the first arb leg pattern and
    `polymarket_yes` + `kalshi_no` the second; the scanner prices both.
    """

    pair_id: str
    kalshi: ResolutionTerms
    polymarket: ResolutionTerms
    verification: Verification = Verification.UNVERIFIED
    verified_by: str = ""
    verified_at: str | None = None
    risk_flags: list[RiskFlag] = field(default_factory=list)
    notes: str = ""

    # Polymarket token ids for the YES and NO outcomes; needed to place or
    # price either leg.
    polymarket_yes_token: str = ""
    polymarket_no_token: str = ""

    @property
    def blocking_flags(self) -> list[RiskFlag]:
        return [f for f in self.risk_flags if f in BLOCKING_FLAGS]

    @property
    def is_tradeable(self) -> bool:
        """
        A pair is tradeable only if a human verified equivalence AND no
        blocking divergence is recorded. Both conditions, deliberately: a
        VERIFIED stamp with a SOURCE_MISMATCH flag is a contradiction, and the
        safe reading of a contradiction is 'do not trade'.
        """
        return self.verification is Verification.VERIFIED and not self.blocking_flags

    def block_reason(self) -> str | None:
        """Human-readable reason this pair is not tradeable, or None."""
        if self.verification is Verification.REJECTED:
            return "pair was reviewed and rejected as non-equivalent"
        if self.verification is Verification.UNVERIFIED:
            return "resolution equivalence has not been verified by a human"
        if self.verification is Verification.EXPIRED:
            return "verification expired — a rulebook changed since review"
        if self.blocking_flags:
            names = ", ".join(f.value for f in self.blocking_flags)
            return f"blocking divergence recorded: {names}"
        return None

    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verification"] = self.verification.value
        d["risk_flags"] = [f.value for f in self.risk_flags]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MarketPair":
        return cls(
            pair_id=d["pair_id"],
            kalshi=ResolutionTerms(**d["kalshi"]),
            polymarket=ResolutionTerms(**d["polymarket"]),
            verification=Verification(d.get("verification", "UNVERIFIED")),
            verified_by=d.get("verified_by", ""),
            verified_at=d.get("verified_at"),
            risk_flags=[RiskFlag(f) for f in d.get("risk_flags", [])],
            notes=d.get("notes", ""),
            polymarket_yes_token=d.get("polymarket_yes_token", ""),
            polymarket_no_token=d.get("polymarket_no_token", ""),
        )

    def mark_verified(self, who: str, *, notes: str = "") -> None:
        if self.blocking_flags:
            raise ValueError(
                f"cannot verify {self.pair_id}: blocking flags present "
                f"({', '.join(f.value for f in self.blocking_flags)}). "
                "Clear the divergence or reject the pair."
            )
        self.verification = Verification.VERIFIED
        self.verified_by = who
        self.verified_at = datetime.now(timezone.utc).isoformat()
        if notes:
            self.notes = notes

    def mark_rejected(self, who: str, flag: RiskFlag, *, notes: str = "") -> None:
        self.verification = Verification.REJECTED
        self.verified_by = who
        self.verified_at = datetime.now(timezone.utc).isoformat()
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)
        if notes:
            self.notes = notes


class PairRegistry:
    """A JSON-backed collection of curated pairs."""

    def __init__(self, pairs: Iterable[MarketPair] = ()) -> None:
        self._pairs: dict[str, MarketPair] = {p.pair_id: p for p in pairs}

    def __len__(self) -> int:
        return len(self._pairs)

    def __iter__(self) -> Iterator[MarketPair]:
        return iter(self._pairs.values())

    def __contains__(self, pair_id: object) -> bool:
        return pair_id in self._pairs

    def get(self, pair_id: str) -> MarketPair | None:
        return self._pairs.get(pair_id)

    def add(self, pair: MarketPair) -> None:
        self._pairs[pair.pair_id] = pair

    def tradeable(self) -> list[MarketPair]:
        return [p for p in self._pairs.values() if p.is_tradeable]

    def needing_review(self) -> list[MarketPair]:
        return [
            p for p in self._pairs.values()
            if p.verification in (Verification.UNVERIFIED, Verification.EXPIRED)
        ]

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "PairRegistry":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(MarketPair.from_dict(d) for d in raw.get("pairs", []))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pairs": [p.to_dict() for p in self._pairs.values()],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
