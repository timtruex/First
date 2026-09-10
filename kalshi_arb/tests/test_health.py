"""
Outage alerting and the liveness digest.

The property under test: silence must never be ambiguous. A scanner finding
nothing and a scanner that died on Tuesday must not look the same.
"""
import asyncio
import json
from decimal import Decimal

import pytest

from daemon import DaemonStats, ScanDaemon
from money import ZERO
from notify import HealthReporter


def run(coro):
    return asyncio.run(coro)


class Recorder:
    name = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def reporter(**kw):
    rec = Recorder()
    kw.setdefault("digest_interval_sec", 86400)
    return HealthReporter([rec], **kw), rec


def spread(pair_id="p", net_profit="10", contracts=100):
    from scanner import Spread
    return Spread(
        pair_id=pair_id, direction="KALSHI_YES_POLY_NO", contracts=contracts,
        kalshi_price=Decimal("0.40"), poly_price=Decimal("0.55"),
        gross_edge_per_contract=Decimal("0.05"), fees_total=Decimal("1"),
        net_profit=Decimal(net_profit), capital_required=Decimal("95"),
        tradeable=True, block_reason=None,
    )


# ----------------------------------------------------------------------
# Failure alerts
# ----------------------------------------------------------------------

def test_single_blip_does_not_alert():
    """One failed cycle is a transient, not an outage."""
    h, rec = reporter(failure_threshold=3)
    assert not h.report_failure(1, "boom")
    assert not h.report_failure(2, "boom")
    assert rec.sent == []


def test_alerts_once_the_threshold_is_reached():
    h, rec = reporter(failure_threshold=3)
    assert h.report_failure(3, "ConnectionError: refused")
    assert len(rec.sent) == 1
    subject, body = rec.sent[0]
    assert "DOWN" in subject
    assert "ConnectionError" in body
    assert "status" in body, "should tell the reader how to investigate"


def test_ongoing_outage_is_rate_limited():
    """A 12-hour outage should be a handful of messages, not 720."""
    h, rec = reporter(failure_threshold=3, failure_cooldown_sec=3600)
    h.report_failure(3, "down", now=0.0)
    for n in range(4, 200):
        h.report_failure(n, "down", now=float(n))
    assert len(rec.sent) == 1


def test_outage_realerts_after_the_cooldown():
    h, rec = reporter(failure_threshold=3, failure_cooldown_sec=100)
    h.report_failure(3, "down", now=0.0)
    assert h.report_failure(50, "down", now=101.0)
    assert len(rec.sent) == 2


def test_recovery_is_reported_once():
    h, rec = reporter(failure_threshold=3)
    h.report_failure(3, "down")
    assert h.report_recovery(5)
    assert "recovered" in rec.sent[-1][0].lower()
    assert not h.report_recovery(5), "no second recovery for the same outage"


def test_recovery_from_an_unannounced_outage_is_silent():
    """
    Reporting recovery from an outage you never heard about would be the first
    you learn of it — noise, not information.
    """
    h, rec = reporter(failure_threshold=3)
    assert not h.report_recovery(2)
    assert rec.sent == []


def test_a_new_outage_after_recovery_alerts_again():
    h, rec = reporter(failure_threshold=3, failure_cooldown_sec=99999)
    h.report_failure(3, "down", now=0.0)
    h.report_recovery(3, now=10.0)
    assert h.report_failure(3, "down again", now=20.0)
    assert len(rec.sent) == 3


# ----------------------------------------------------------------------
# Digest
# ----------------------------------------------------------------------

def test_first_call_starts_the_clock_rather_than_firing():
    """A restart must not produce a digest every time the process starts."""
    h, rec = reporter(digest_interval_sec=100)
    assert not h.digest_due(now=1000.0)


def test_digest_due_after_the_interval():
    h, _ = reporter(digest_interval_sec=100)
    h.digest_due(now=0.0)           # starts the clock
    assert not h.digest_due(now=99.0)
    assert h.digest_due(now=100.0)


def test_sending_resets_the_interval():
    h, rec = reporter(digest_interval_sec=100)
    h.digest_due(now=0.0)
    h.send_digest("body", now=100.0)
    assert not h.digest_due(now=150.0)
    assert h.digest_due(now=200.0)
    assert len(rec.sent) == 1


def test_digest_timing_survives_restart(tmp_path):
    """
    Without persistence a process restarted more often than the digest
    interval would never send one — the failure would hide exactly when
    restarts are frequent.
    """
    path = tmp_path / "health.json"
    h1 = HealthReporter([Recorder()], digest_interval_sec=100, state_path=path)
    h1.digest_due(now=0.0)
    h1.send_digest("first", now=100.0)

    rec = Recorder()
    h2 = HealthReporter([rec], digest_interval_sec=100, state_path=path)
    assert not h2.digest_due(now=150.0), "restart must not reset the clock"
    assert h2.digest_due(now=201.0)


def test_failure_state_survives_restart(tmp_path):
    path = tmp_path / "health.json"
    h1 = HealthReporter([Recorder()], failure_threshold=3,
                        failure_cooldown_sec=3600, state_path=path)
    h1.report_failure(3, "down", now=0.0)

    rec = Recorder()
    h2 = HealthReporter([rec], failure_threshold=3,
                        failure_cooldown_sec=3600, state_path=path)
    assert not h2.report_failure(4, "down", now=10.0), "cooldown must persist"
    assert len(rec.sent) == 0


def test_corrupt_health_state_is_survivable(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{not json")
    h, rec = reporter(failure_threshold=1)
    h._state_path = path
    assert h.report_failure(1, "down")


def test_one_failing_channel_does_not_block_others():
    class Broken:
        name = "broken"

        def send(self, s, b):
            raise RuntimeError("down")

    good = Recorder()
    h = HealthReporter([Broken(), good], failure_threshold=1)
    h.report_failure(1, "x")
    assert len(good.sent) == 1


# ----------------------------------------------------------------------
# Digest content
# ----------------------------------------------------------------------

def test_digest_reports_a_quiet_period_rather_than_saying_nothing():
    """The whole point: a boring digest is the signal that it is alive."""
    stats = DaemonStats()
    stats.period_cycles = 1440
    body = stats.digest_body()
    assert "1440" in body
    assert "none above threshold" in body
    assert "healthy" in body


def test_digest_reports_best_edge_and_pair():
    stats = DaemonStats()
    stats.note_spreads([spread(pair_id="a", net_profit="10"),
                        spread(pair_id="b", net_profit="42")])
    body = stats.digest_body()
    assert stats.period_best_pair == "b"
    assert "0.42" in body


def test_digest_surfaces_an_ongoing_failure():
    stats = DaemonStats()
    stats.consecutive_failures = 7
    stats.last_error = "ConnectionError: refused"
    body = stats.digest_body()
    assert "FAILING" in body and "7" in body
    assert "ConnectionError" in body


def test_period_resets_but_lifetime_totals_persist():
    stats = DaemonStats()
    stats.note_spreads([spread()])
    stats.period_cycles = 10
    stats.reset_period()
    assert stats.period_spreads == 0
    assert stats.period_best_edge == ZERO
    assert stats.spreads_found == 1, "lifetime total must survive the reset"


# ----------------------------------------------------------------------
# Daemon wiring
# ----------------------------------------------------------------------

def test_daemon_calls_failure_and_recovery_hooks():
    calls = []
    n = {"i": 0}

    async def scan():
        n["i"] += 1
        if n["i"] <= 3:
            raise RuntimeError("venue down")
        return []

    d = ScanDaemon(
        scan, interval_sec=0.01, max_backoff_sec=0.02,
        on_failure=lambda streak, err: calls.append(("fail", streak)),
        on_recovery=lambda failed: calls.append(("recover", failed)),
        max_cycles=1,
    )
    run(d.run())
    assert [c[0] for c in calls] == ["fail", "fail", "fail", "recover"]
    assert calls[-1] == ("recover", 3)


def test_daemon_emits_digest_when_due_and_resets_the_period():
    bodies = []
    due = {"flag": False}

    async def scan():
        return []

    d = ScanDaemon(
        scan, interval_sec=0.01,
        digest_due=lambda: due["flag"],
        on_digest=bodies.append,
        max_cycles=4,
    )

    async def scenario():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.015)
        due["flag"] = True
        await task

    run(scenario())
    assert len(bodies) >= 1
    assert "Cycles" in bodies[0]


def test_broken_health_hook_does_not_crash_the_daemon():
    def explode(*_a):
        raise RuntimeError("telegram down")

    async def scan():
        raise RuntimeError("venue down")

    d = ScanDaemon(scan, interval_sec=0.01, max_backoff_sec=0.01, on_failure=explode)

    async def scenario():
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.05)
        d.request_stop("test")
        await task

    run(scenario())
    assert d.stats.failures > 0, "loop kept running despite the broken channel"


def test_heartbeat_includes_period_counters(tmp_path):
    hb = tmp_path / "hb.json"

    async def scan():
        return []

    d = ScanDaemon(scan, interval_sec=0.01, heartbeat_path=hb, max_cycles=2)
    run(d.run())
    stats = json.loads(hb.read_text())
    assert stats["period_cycles"] == 2
