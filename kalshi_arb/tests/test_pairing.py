"""Resolution-equivalence gating — the guard that separates arb from a coin flip."""
import pytest

from pairing import (
    BLOCKING_FLAGS, MarketPair, PairRegistry, ResolutionTerms, RiskFlag, Verification,
)


def pair(**kw) -> MarketPair:
    return MarketPair(
        pair_id=kw.pop("pair_id", "p1"),
        kalshi=ResolutionTerms("kalshi", "K1", "Kalshi question"),
        polymarket=ResolutionTerms("polymarket", "0xP1", "Poly question"),
        **kw,
    )


def test_new_pairs_are_untradeable_by_default():
    """Fail-closed: an unreviewed pair must never be tradeable."""
    p = pair()
    assert p.verification is Verification.UNVERIFIED
    assert not p.is_tradeable
    assert "verified" in p.block_reason()


def test_verification_makes_a_clean_pair_tradeable():
    p = pair()
    p.mark_verified("tim")
    assert p.is_tradeable
    assert p.block_reason() is None
    assert p.verified_by == "tim"
    assert p.verified_at is not None


@pytest.mark.parametrize("flag", sorted(BLOCKING_FLAGS, key=lambda f: f.value))
def test_every_blocking_flag_blocks_even_after_verification(flag):
    p = pair()
    p.mark_verified("tim")
    p.risk_flags.append(flag)
    assert not p.is_tradeable
    assert flag.value in p.block_reason()


def test_non_blocking_flag_is_recorded_without_blocking():
    """Settlement lag is a funding cost, not a divergence in payout."""
    p = pair()
    p.mark_verified("tim")
    p.risk_flags.append(RiskFlag.SETTLEMENT_LAG)
    assert p.is_tradeable


def test_cannot_verify_a_pair_with_a_blocking_flag():
    p = pair()
    p.risk_flags.append(RiskFlag.SOURCE_MISMATCH)
    with pytest.raises(ValueError, match="blocking flags"):
        p.mark_verified("tim")
    assert not p.is_tradeable


def test_rejected_pair_stays_blocked():
    p = pair()
    p.mark_rejected("tim", RiskFlag.EDGE_CASE_WORDING, notes="postponement differs")
    assert p.verification is Verification.REJECTED
    assert not p.is_tradeable
    assert "rejected" in p.block_reason()


def test_expired_verification_blocks():
    p = pair(verification=Verification.EXPIRED)
    assert not p.is_tradeable
    assert "expired" in p.block_reason()


def test_roundtrip_through_json_preserves_gating(tmp_path):
    reg = PairRegistry()
    good = pair(pair_id="good")
    good.mark_verified("tim")
    bad = pair(pair_id="bad")
    bad.mark_rejected("tim", RiskFlag.SOURCE_MISMATCH)
    reg.add(good)
    reg.add(bad)
    reg.add(pair(pair_id="new"))

    path = tmp_path / "pairs.json"
    reg.save(path)
    loaded = PairRegistry.load(path)

    assert len(loaded) == 3
    assert [p.pair_id for p in loaded.tradeable()] == ["good"]
    assert [p.pair_id for p in loaded.needing_review()] == ["new"]
    assert loaded.get("bad").verification is Verification.REJECTED
    assert RiskFlag.SOURCE_MISMATCH in loaded.get("bad").risk_flags


def test_loading_a_missing_file_is_empty_not_an_error():
    assert len(PairRegistry.load(__import__("pathlib").Path("/nonexistent/pairs.json"))) == 0
