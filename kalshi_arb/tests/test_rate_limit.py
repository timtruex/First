"""Token bucket: weights charged correctly, limits actually enforced."""
import asyncio

import pytest

from rate_limit import DEFAULT_WEIGHTS, RateLimitExceeded, TokenBucket, WeightedLimiter


def run(coro):
    return asyncio.run(coro)


def test_starts_full_and_spends_by_weight():
    b = TokenBucket(capacity=100, refill_per_second=10)
    assert b.available == pytest.approx(100, abs=0.1)
    run(b.acquire(15))
    assert b.available == pytest.approx(85, abs=0.1)


def test_rejects_invalid_configuration():
    for cap, rate in ((0, 10), (10, 0), (-1, 10)):
        with pytest.raises(ValueError):
            TokenBucket(capacity=cap, refill_per_second=rate)


def test_weight_larger_than_capacity_raises_rather_than_hanging():
    b = TokenBucket(capacity=10, refill_per_second=1)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        run(b.acquire(11))


def test_zero_weight_is_free():
    b = TokenBucket(capacity=10, refill_per_second=1)
    assert run(b.acquire(0)) == 0.0
    assert b.available == pytest.approx(10, abs=0.1)


def test_timeout_raises_instead_of_waiting_forever():
    b = TokenBucket(capacity=15, refill_per_second=0.1)
    run(b.acquire(15))
    with pytest.raises(RateLimitExceeded):
        run(b.acquire(15, timeout=0.05))


def test_refill_is_continuous_not_windowed():
    """Windowed refill allows a double-rate burst at the window boundary."""
    async def scenario():
        b = TokenBucket(capacity=100, refill_per_second=100)
        await b.acquire(100)
        await asyncio.sleep(0.05)
        return b.available
    got = run(scenario())
    assert 2 < got < 20, f"expected partial continuous refill, got {got}"


def test_blocks_until_refilled_then_proceeds():
    async def scenario():
        b = TokenBucket(capacity=10, refill_per_second=100)
        await b.acquire(10)
        waited = await b.acquire(10, timeout=5)
        return waited
    waited = run(scenario())
    assert waited > 0.05


def test_concurrent_callers_cannot_double_spend():
    """Without the lock both coroutines see the same free capacity."""
    async def scenario():
        b = TokenBucket(capacity=30, refill_per_second=1000)
        await asyncio.gather(*(b.acquire(10, timeout=5) for _ in range(3)))
        return b.available
    remaining = run(scenario())
    assert remaining < 5


def test_order_mutation_weight_is_fifteen():
    lim = WeightedLimiter(capacity=100, refill_per_second=10)
    for op in ("order_create", "order_amend", "order_decrease", "order_cancel"):
        assert lim.weight_for(op) == 15
    assert lim.weight_for("read") == 1


def test_unknown_operation_charges_max_weight_not_one():
    """Failing open on the mutation path is the dangerous direction."""
    lim = WeightedLimiter(capacity=100, refill_per_second=10)
    assert lim.weight_for("typo_create_ordr") == max(DEFAULT_WEIGHTS.values())


def test_throttled_seconds_accumulates():
    async def scenario():
        lim = WeightedLimiter(capacity=15, refill_per_second=100)
        await lim.acquire("order_create")
        await lim.acquire("order_create", timeout=5)
        return lim.throttled_seconds
    assert run(scenario()) > 0


def test_budget_math_matches_weights():
    """A 100-token bucket admits 6 order mutations, not 100 requests."""
    async def scenario():
        lim = WeightedLimiter(capacity=100, refill_per_second=0.0001)
        admitted = 0
        for _ in range(20):
            try:
                await lim.acquire("order_create", timeout=0)
            except RateLimitExceeded:
                break
            admitted += 1
        return admitted
    assert run(scenario()) == 6
