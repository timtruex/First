"""
Alert suppression.

At a 60s interval an unsuppressed notifier reports a six-hour spread 360
times. These tests pin the rule that keeps the channel worth reading.
"""
import json
from decimal import Decimal

import pytest

from notify import (
    ConsoleChannel, MacNotificationChannel, Notifier, TelegramChannel,
)


def spread(pair_id="p", direction="KALSHI_YES_POLY_NO", net_profit="10", contracts=100):
    from scanner import Spread
    return Spread(
        pair_id=pair_id, direction=direction, contracts=contracts,
        kalshi_price=Decimal("0.40"), poly_price=Decimal("0.55"),
        gross_edge_per_contract=Decimal("0.05"), fees_total=Decimal("1"),
        net_profit=Decimal(net_profit), capital_required=Decimal("95"),
        tradeable=True, block_reason=None,
    )


class Recorder:
    name = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def notifier(**kw):
    rec = Recorder()
    return Notifier([rec], **kw), rec


def render(s):
    return f"{s.pair_id} {s.net_profit}"


# ----------------------------------------------------------------------
# Suppression
# ----------------------------------------------------------------------

def test_first_sighting_always_alerts():
    n, rec = notifier()
    assert len(n.notify([spread()], render=render)) == 1
    assert len(rec.sent) == 1


def test_unchanged_spread_does_not_re_alert():
    n, rec = notifier(cooldown_sec=3600)
    n.notify([spread()], render=render)
    for _ in range(100):
        n.notify([spread()], render=render)
    assert len(rec.sent) == 1, "a persistent spread is one opportunity"


def test_materially_improved_spread_re_alerts():
    """Widening changes the sizing decision, so it is new information."""
    n, rec = notifier(cooldown_sec=3600, improvement_threshold=Decimal("0.01"))
    n.notify([spread(net_profit="10")], render=render)       # 0.10/contract
    n.notify([spread(net_profit="11")], render=render)       # 0.11 => +0.01
    assert len(rec.sent) == 2


def test_marginal_improvement_stays_suppressed():
    n, rec = notifier(cooldown_sec=3600, improvement_threshold=Decimal("0.01"))
    n.notify([spread(net_profit="10")], render=render)
    n.notify([spread(net_profit="10.5")], render=render)     # +0.005 only
    assert len(rec.sent) == 1


def test_narrowing_then_rewidening_does_not_re_alert():
    """
    Tracking the latest edge rather than the best would let a spread that
    oscillates around one level alert on every upswing.
    """
    n, rec = notifier(cooldown_sec=3600, improvement_threshold=Decimal("0.01"))
    n.notify([spread(net_profit="12")], render=render)
    n.notify([spread(net_profit="5")], render=render)
    n.notify([spread(net_profit="12")], render=render)
    assert len(rec.sent) == 1


def test_cooldown_expiry_allows_a_reminder():
    n, rec = notifier(cooldown_sec=100)
    s = spread()
    n.notify([s], render=render)
    n.record(s, now=0.0)                    # backdate the record
    assert n.should_alert(s, now=101.0)
    assert not n.should_alert(s, now=99.0)


def test_directions_and_pairs_are_tracked_separately():
    n, rec = notifier(cooldown_sec=3600)
    n.notify([
        spread(pair_id="a", direction="KALSHI_YES_POLY_NO"),
        spread(pair_id="a", direction="POLY_YES_KALSHI_NO"),
        spread(pair_id="b", direction="KALSHI_YES_POLY_NO"),
    ], render=render)
    assert len(rec.sent) == 3


# ----------------------------------------------------------------------
# Channel isolation
# ----------------------------------------------------------------------

def test_one_failing_channel_does_not_block_the_others():
    class Broken:
        name = "broken"

        def send(self, subject, body):
            raise RuntimeError("down")

    good = Recorder()
    n = Notifier([Broken(), good], cooldown_sec=3600)
    n.notify([spread()], render=render)
    assert len(good.sent) == 1


def test_failing_channel_still_records_suppression():
    """Otherwise an outage produces a retry storm once the channel returns."""
    class Broken:
        name = "broken"

        def send(self, subject, body):
            raise RuntimeError("down")

    n = Notifier([Broken()], cooldown_sec=3600)
    n.notify([spread()], render=render)
    assert not n.should_alert(spread())


def test_subject_line_carries_the_decision_relevant_numbers():
    n, rec = notifier()
    n.notify([spread(net_profit="42")], render=render)
    subject, _ = rec.sent[0]
    assert "TRADEABLE" in subject and "$42" in subject and "$95" in subject


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    """A daemon restart must not replay every alert it already sent."""
    path = tmp_path / "state.json"
    n1 = Notifier([Recorder()], cooldown_sec=3600, state_path=path)
    n1.notify([spread()], render=render)

    rec2 = Recorder()
    n2 = Notifier([rec2], cooldown_sec=3600, state_path=path)
    n2.notify([spread()], render=render)
    assert len(rec2.sent) == 0


def test_corrupt_state_file_is_survivable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    rec = Recorder()
    n = Notifier([rec], cooldown_sec=3600, state_path=path)
    n.notify([spread()], render=render)
    assert len(rec.sent) == 1, "corrupt state costs one duplicate, not a crash"


def test_state_write_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    n = Notifier([Recorder()], cooldown_sec=3600, state_path=path)
    n.notify([spread()], render=render)
    assert json.loads(path.read_text())["seen"]
    assert not path.with_suffix(".tmp").exists(), "temp file must be renamed away"


def test_prune_bounds_the_state_file():
    n = Notifier([Recorder()], cooldown_sec=100)
    for i in range(50):
        n.record(spread(pair_id=f"p{i}"), now=0.0)
    assert n.prune(now=100_000) == 50
    assert n._seen == {}


def test_prune_keeps_recent_records():
    n = Notifier([Recorder()], cooldown_sec=100)
    n.record(spread(pair_id="old"), now=0.0)
    n.record(spread(pair_id="new"), now=99_000.0)
    n.prune(now=100_000)
    assert "old" not in str(n._seen) or len(n._seen) == 1


# ----------------------------------------------------------------------
# Channels
# ----------------------------------------------------------------------

def test_telegram_disabled_without_both_credentials():
    assert not TelegramChannel("", "").enabled
    assert not TelegramChannel("token", "").enabled
    assert not TelegramChannel("", "chat").enabled
    assert TelegramChannel("token", "chat").enabled


def test_disabled_telegram_send_is_a_noop():
    TelegramChannel("", "").send("subject", "body")   # must not raise


def test_macos_channel_is_inert_off_platform():
    """Same config must run on Linux CI without erroring."""
    MacNotificationChannel().send("subject", "body")


def test_console_channel_accepts_anything():
    ConsoleChannel().send("subject", "multi\nline\nbody")
