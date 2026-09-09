"""
Normalising each venue's market payload into resolution terms.

Two jobs, both of which were previously left to the operator and are the
reason a suggested pair could not actually be scanned:

  1. Extract the Polymarket CLOB token ids. Books are keyed by outcome token,
     not by condition id, so a pair without them is silently unscannable.
  2. Pull the fields a human needs to judge resolution equivalence — close
     time, resolution source, rules text — so verification does not require
     opening two browser tabs and copying text by hand.

Outcome-to-token mapping is by name, never by position
------------------------------------------------------
Gamma returns `outcomes` and `clobTokenIds` as parallel arrays. Assuming
index 0 is YES is wrong often enough to matter: the arrays are ordered as the
market was created, and a market phrased negatively, or one with non-binary
outcome labels, will silently invert. An inverted pair does not error — it
prices the NO book as YES and reports a spread that is the exact opposite of
reality, which is the most expensive kind of bug this scanner could have. So
the mapping is by label, and anything that does not resolve to a clean
Yes/No pair is refused rather than guessed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pairing import ResolutionTerms

logger = logging.getLogger(__name__)

_YES_LABELS = {"yes", "y", "true"}
_NO_LABELS = {"no", "n", "false"}


class MetadataError(ValueError):
    """Raised when a market payload cannot be turned into usable terms."""


def _maybe_json_list(value: Any, *, field: str) -> list:
    """
    Gamma returns these arrays as JSON-encoded strings, but not always.

    Accepting both shapes rather than assuming one keeps a payload-format
    change from silently producing empty tokens.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MetadataError(f"{field}: not valid JSON: {text[:80]!r}") from exc
        if not isinstance(parsed, list):
            raise MetadataError(f"{field}: expected a list, got {type(parsed).__name__}")
        return parsed
    raise MetadataError(f"{field}: unsupported type {type(value).__name__}")


def _first(d: dict, *keys: str, default: str = "") -> str:
    """Take the first present, non-empty key. Both APIs vary their casing."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return str(v)
    return default


def _excerpt(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


# ----------------------------------------------------------------------
# Kalshi
# ----------------------------------------------------------------------

def kalshi_terms(market: dict) -> ResolutionTerms:
    ticker = _first(market, "ticker")
    if not ticker:
        raise MetadataError("kalshi market has no ticker")

    rules = "\n\n".join(
        part for part in (
            _first(market, "rules_primary"),
            _first(market, "rules_secondary"),
        ) if part
    )
    return ResolutionTerms(
        venue="kalshi",
        market_id=ticker,
        title=_first(market, "title", "subtitle", default=ticker),
        resolution_source=_first(market, "settlement_source", "source"),
        close_time=_first(market, "close_time") or None,
        expected_settlement=_first(market, "expiration_time", "settlement_time") or None,
        rules_url=f"https://kalshi.com/markets/{ticker}",
        rules_excerpt=_excerpt(rules),
    )


# ----------------------------------------------------------------------
# Polymarket
# ----------------------------------------------------------------------

def polymarket_tokens(market: dict) -> tuple[str, str]:
    """
    Return (yes_token, no_token), mapped by outcome label.

    Raises rather than guessing: a silently inverted pair reports the exact
    opposite of the real spread.
    """
    outcomes = _maybe_json_list(market.get("outcomes"), field="outcomes")
    tokens = _maybe_json_list(market.get("clobTokenIds") or market.get("clob_token_ids"),
                              field="clobTokenIds")

    if not tokens:
        raise MetadataError("market has no clobTokenIds — cannot fetch its books")
    if len(outcomes) != len(tokens):
        raise MetadataError(
            f"outcomes/token count mismatch ({len(outcomes)} vs {len(tokens)}); "
            "refusing to guess the mapping"
        )
    if len(tokens) != 2:
        raise MetadataError(
            f"expected a binary market, got {len(tokens)} outcomes: {outcomes}"
        )

    mapping: dict[str, str] = {}
    for label, token in zip(outcomes, tokens):
        key = str(label).strip().lower()
        if key in _YES_LABELS:
            mapping["yes"] = str(token)
        elif key in _NO_LABELS:
            mapping["no"] = str(token)

    if "yes" not in mapping or "no" not in mapping:
        raise MetadataError(
            f"outcomes {outcomes} are not a Yes/No pair; this scanner only "
            "handles binary markets, and mapping by position would risk "
            "inverting the book"
        )
    return mapping["yes"], mapping["no"]


def polymarket_terms(market: dict) -> ResolutionTerms:
    condition = _first(market, "conditionId", "condition_id", "id")
    if not condition:
        raise MetadataError("polymarket market has no conditionId")

    slug = _first(market, "slug")
    return ResolutionTerms(
        venue="polymarket",
        market_id=condition,
        title=_first(market, "question", "title", default=condition),
        resolution_source=_first(market, "resolutionSource", "resolution_source"),
        close_time=_first(market, "endDate", "end_date_iso", "endDateIso") or None,
        expected_settlement=_first(market, "umaEndDate") or None,
        rules_url=f"https://polymarket.com/event/{slug}" if slug else "",
        rules_excerpt=_excerpt(_first(market, "description")),
    )


def enrich_pair(pair, kalshi_market: dict | None, polymarket_market: dict | None) -> list[str]:
    """
    Populate a pair's terms and tokens in place.

    Returns a list of problems rather than raising: one unusable market should
    downgrade that pair, not abort a refresh over the whole registry.
    """
    problems: list[str] = []

    if kalshi_market:
        try:
            pair.kalshi = kalshi_terms(kalshi_market)
        except MetadataError as exc:
            problems.append(f"kalshi: {exc}")

    if polymarket_market:
        try:
            pair.polymarket = polymarket_terms(polymarket_market)
        except MetadataError as exc:
            problems.append(f"polymarket: {exc}")
        try:
            yes, no = polymarket_tokens(polymarket_market)
            pair.polymarket_yes_token = yes
            pair.polymarket_no_token = no
        except MetadataError as exc:
            problems.append(f"polymarket tokens: {exc}")

    return problems
