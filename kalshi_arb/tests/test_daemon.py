"""
Supervision loop behaviour.

The scan callable is injected, so every property here is tested without a
network: backoff, shutdown latency, heartbeat, and failure isolation.
"""
import asyncio
import json
from decimal import Decimal

import pytest

from daemon import DEFAULT_MAX_BACKOFF_SEC, ScanDaemon, backoff_delay


def run(coro):
    return asyncio.run(coro)


def spread(pair_id="p", profit="10"):
    from scanner import Spread
    return Spread(
        pair_id=pair_id, direction="KALSHI_YES_POLY_NO", contracts=100,
        kalshi_price=Decimal("0.40"), poly_price=Decimal("0.55"),
        gross_edge_per_contract=Decimal("0.05"), fees_total=Decimal("1"),
        net_profit=Decimal(profit), capital_required=Decimal("95"),
        tradeable=True, block_reason=None,
    )


# ----------------------------------------------------------------------
# Backoff
# ----------------------------------------------------------------------

def test_backoff_grows_exponentially_then_caps():
    delays = [backoff_delay(n, base=60, cap=900) for n in range(1, 8)]
    assert delays[:4] == [120, 240, 480, 900]
    assert all(d <= 900 for d in delays)
    assert delays == sorted(delays), "backoff must be monotonic"


def test_no_failures_uses_the_base_interval():
    assert backoff_delay(0, base=60, cap=900) == 60


# ----------------------------------------------------------------------
# Cycle accounting
# ----------------------------------------------------------------------

def test_successful_cycles_are_counted():
    async def scan():
        return [spread()]
    d = ScanDaemon(scan, interval_sec=0.01, max_cycles=3)
    stats = run(d.run())
    assert stats.cycles == 3
    assert stats.failures == 0
    assert stats.spreads_found == 3


def test_failures_do_not_stop_the_loop():
    """One venue outage must not end a week-long run."""
    calls = {"n": 0}

    async def scan():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("venue down")
        return [spread()]

    d = ScanDaemon(scan, interval_sec=0.01, max_backoff_sec=0.05, max_cycles=2)
    stats = run(d.run())
    assert stats.failures == 2
    assert stats.cycles == 2
    assert stats.consecutive_failures == 0, "counter must reset on recovery"


def test_consecutive_failure_counter_resets_on_success():
    calls = {"n": 0}

    async def scan():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return []

    d = ScanDaemon(scan, interval_sec=0.01, max_backoff_sec=0.02, max_cycles=1)
    stats = run(d.run())
    assert stats.consecutive_failures == 0
    assert stats.last_error is None


def test_last_error_is_recorded_for_diagnosis():
    async def scan():
        raise ValueError("boom")

    d = ScanDaemon(scan, interval_sec=0.01, max_backoff_sec=0.01)

    async def scenario():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.05)
        d.request_stop("test")
        await task

    run(scenario())
    assert "ValueError" in d.stats.last_error
    assert "boom" in d.stats.last_error


def test_alert_dispatch_failure_does_not_kill_the_cycle():
    def bad_dispatch(_spreads):
        raise RuntimeError("telegram down")

    async def scan():
        return [spread()]

    d = ScanDaemon(scan, on_spreads=bad_dispatch, interval_sec=0.01, max_cycles=2)
    stats = run(d.run())
    assert stats.cycles == 2
    assert stats.failures == 0, "a broken alert channel is not a scan failure"


def test_alert_counts_accumulate():
    async def scan():
        return [spread(), spread("q")]

    d = ScanDaemon(scan, on_spreads=lambda s: len(s), interval_sec=0.01, max_cycles=2)
    assert run(d.run()).alerts_sent == 4


# ----------------------------------------------------------------------
# Shutdown
# ----------------------------------------------------------------------

def test_stop_is_honoured_without_waiting_a_full_interval():
    """
    launchd SIGKILLs after its grace period. Sleeping the interval instead of
    waiting on the stop event is what gets a process killed mid-request.
    """
    async def scan():
        return []

    d = ScanDaemon(scan, interval_sec=30.0)

    async def scenario():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.05)
        d.request_stop("test")
        await asyncio.wait_for(task, timeout=1.0)

    run(scenario())   # would time out if the loop slept the full 30s
    assert d.stats.cycles >= 1


def test_repeated_stop_requests_are_idempotent():
    async def scan():
        return []
    d = ScanDaemon(scan, interval_sec=0.01, max_cycles=1)
    d.request_stop("first")
    d.request_stop("second")
    run(d.run())


# ----------------------------------------------------------------------
# Heartbeat
# ----------------------------------------------------------------------

def test_heartbeat_written_and_parseable(tmp_path):
    hb = tmp_path / "hb.json"

    async def scan():
        return [spread()]

    d = ScanDaemon(scan, interval_sec=0.01, heartbeat_path=hb, max_cycles=2)
    run(d.run())

    stats = json.loads(hb.read_text())
    assert stats["cycles"] == 2
    assert stats["last_cycle_at"] is not None
    assert stats["last_error"] is None


def test_heartbeat_records_failures_too():
    """A wedged daemon is only observable if failures reach the heartbeat."""
    import tempfile
    from pathlib import Path
    hb = Path(tempfile.mkdtemp()) / "hb.json"

    async def scan():
        raise RuntimeError("down")

    d = ScanDaemon(scan, interval_sec=0.01, max_backoff_sec=0.01, heartbeat_path=hb)

    async def scenario():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.06)
        d.request_stop("test")
        await task

    run(scenario())
    stats = json.loads(hb.read_text())
    assert stats["failures"] > 0
    assert "RuntimeError" in stats["last_error"]


def test_unwritable_heartbeat_does_not_stop_the_daemon(tmp_path):
    """Observability must never take down the thing being observed."""
    async def scan():
        return []

    d = ScanDaemon(scan, interval_sec=0.01,
                   heartbeat_path=tmp_path / "nope" / "\0bad" / "hb.json",
                   max_cycles=2)
    assert run(d.run()).cycles == 2
