"""
Alert delivery and suppression.

Running a scanner 24/7 makes suppression the hard part, not delivery. A spread
that persists for six hours is *one* opportunity; at a 30-second scan interval
a naive notifier reports it 720 times, and the practical result is that you
stop reading the alerts — which costs you the one that mattered.

So an alert fires when there is genuinely new information:

  - the (pair, direction) has not alerted inside the cooldown window, OR
  - its net edge per contract improved by at least `improvement_threshold`
    since the last alert on that key

Widening is new information because it changes the sizing decision; a spread
drifting a hundredth of a cent is not. The threshold is per-contract rather
than total so it does not fire merely because more depth appeared at the same
price.

Channels are best-effort and independent: a Telegram outage must never stop
the loop or suppress the console line, so every channel failure is caught and
logged rather than raised.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from money import fmt_usd
from scanner import Spread

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SEC = 3600.0
DEFAULT_IMPROVEMENT = Decimal("0.01")   # one cent per contract


class Channel(Protocol):
    name: str

    def send(self, subject: str, body: str) -> None: ...


@dataclass
class ConsoleChannel:
    name: str = "console"

    def send(self, subject: str, body: str) -> None:
        logger.info("ALERT %s\n%s", subject, body)


@dataclass
class MacNotificationChannel:
    """
    Native macOS banner via osascript.

    Present because the target runtime is a Mac mini; it degrades to a no-op
    everywhere else rather than erroring, so the same config runs on Linux CI.
    """

    name: str = "macos"
    _available: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._available = shutil.which("osascript") is not None
        if not self._available:
            logger.debug("osascript not found; macOS notifications disabled")

    def send(self, subject: str, body: str) -> None:
        if not self._available:
            return
        # First line only: banners truncate, and a multi-line body renders as
        # unreadable run-on text.
        first = body.strip().splitlines()[0] if body.strip() else ""
        script = (
            f'display notification {json.dumps(first)} '
            f'with title {json.dumps(subject)}'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False, capture_output=True, timeout=5,
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("macOS notification failed: %s", exc)


@dataclass
class TelegramChannel:
    """Optional Telegram push. Disabled unless both token and chat id are set."""

    token: str = ""
    chat_id: str = ""
    name: str = "telegram"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, subject: str, body: str) -> None:
        if not self.enabled:
            return
        try:
            import urllib.parse
            import urllib.request

            payload = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": f"*{subject}*\n```\n{body}\n```",
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage", data=payload
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            logger.warning("Telegram alert failed: %s", exc)


@dataclass
class _AlertRecord:
    last_sent: float
    best_edge: Decimal


class Notifier:
    """Routes spreads to channels, suppressing repeats."""

    def __init__(
        self,
        channels: list[Channel],
        *,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        improvement_threshold: Decimal = DEFAULT_IMPROVEMENT,
        state_path: Path | None = None,
    ) -> None:
        self._channels = channels
        self._cooldown = cooldown_sec
        self._improvement = improvement_threshold
        self._state_path = state_path
        self._seen: dict[str, _AlertRecord] = {}
        self._load()

    @staticmethod
    def _key(spread: Spread) -> str:
        return f"{spread.pair_id}::{spread.direction}"

    def should_alert(self, spread: Spread, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        rec = self._seen.get(self._key(spread))
        if rec is None:
            return True
        if now - rec.last_sent >= self._cooldown:
            return True
        return spread.net_edge_per_contract - rec.best_edge >= self._improvement

    def record(self, spread: Spread, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = self._key(spread)
        prev = self._seen.get(key)
        # Track the best edge seen this cycle, not the latest: otherwise a
        # spread that narrows then re-widens to its old level re-alerts as if
        # it had improved.
        best = spread.net_edge_per_contract
        if prev is not None:
            best = max(best, prev.best_edge)
        self._seen[key] = _AlertRecord(last_sent=now, best_edge=best)

    def prune(self, *, now: float | None = None, max_age: float | None = None) -> int:
        """Drop records past their usefulness so the state file cannot grow forever."""
        now = time.time() if now is None else now
        horizon = max_age if max_age is not None else self._cooldown * 24
        stale = [k for k, r in self._seen.items() if now - r.last_sent > horizon]
        for k in stale:
            del self._seen[k]
        return len(stale)

    def notify(self, spreads: list[Spread], *, render) -> list[Spread]:
        """Send alerts for spreads that clear suppression. Returns those sent."""
        sent: list[Spread] = []
        for s in spreads:
            if not self.should_alert(s):
                continue
            subject = (
                f"{'TRADEABLE' if s.tradeable else 'RESEARCH'} "
                f"{s.pair_id} · {fmt_usd(s.net_profit)} on {fmt_usd(s.capital_required)}"
            )
            body = render(s)
            for ch in self._channels:
                try:
                    ch.send(subject, body)
                except Exception as exc:
                    logger.warning("Channel %s failed: %s", ch.name, exc)
            self.record(s)
            sent.append(s)
        if sent:
            self.prune()
            self._save()
        return sent

    # ------------------------------------------------------------------
    # Suppression state survives restarts
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
            self._seen = {
                k: _AlertRecord(last_sent=float(v["last_sent"]), best_edge=Decimal(v["best_edge"]))
                for k, v in raw.get("seen", {}).items()
            }
        except Exception as exc:
            # A corrupt state file must not stop the daemon; the cost of
            # losing it is one duplicate alert per key.
            logger.warning("Could not read alert state (%s); starting fresh", exc)
            self._seen = {}

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "seen": {
                    k: {"last_sent": r.last_sent, "best_edge": str(r.best_edge)}
                    for k, r in self._seen.items()
                }
            }, indent=2))
            # Atomic replace: a crash mid-write must not leave a truncated file
            # that fails to parse on the next start.
            tmp.replace(self._state_path)
        except Exception as exc:
            logger.warning("Could not persist alert state: %s", exc)


# ----------------------------------------------------------------------
# Health reporting: outages and the periodic digest
# ----------------------------------------------------------------------

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_FAILURE_COOLDOWN_SEC = 3600.0
DEFAULT_DIGEST_INTERVAL_SEC = 86400.0


@dataclass
class _HealthState:
    last_failure_alert_at: float = 0.0
    alerted_streak: int = 0
    # None means "never sent". A 0.0 sentinel would collide with a legitimate
    # timestamp and restart the digest clock on every check.
    last_digest_at: float | None = None


class HealthReporter:
    """
    Pushes outage and liveness information, which spread alerts alone cannot.

    Spread alerts are silent by design when there is nothing to report, so
    silence carries no information: a scanner finding nothing and a scanner
    that died on Tuesday look identical from the outside. Two mechanisms fix
    that from opposite directions.

    Failure alerts tell you something broke. They fire once the failure streak
    reaches `failure_threshold` — three consecutive cycles, so roughly three
    minutes at the default interval: long enough not to fire on a single
    transient blip, short enough that you hear about a real outage quickly.
    While the outage continues the alert repeats only on a cooldown, because a
    twelve-hour outage should be a handful of messages, not 720. Recovery is
    reported once, so an outage always has a visible end.

    The digest tells you it is still alive. It sends on a fixed interval
    whether or not anything happened — that is the entire point, since a
    digest that only sends interesting news reintroduces the ambiguity it
    exists to remove. Its absence becomes the alarm.

    Digest timing is persisted. Without that, a process restarted more often
    than the digest interval would never emit one at all: the failure mode
    would hide precisely when restarts are frequent, which is exactly when you
    most want to hear from it.
    """

    def __init__(
        self,
        channels: list[Channel],
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        failure_cooldown_sec: float = DEFAULT_FAILURE_COOLDOWN_SEC,
        digest_interval_sec: float = DEFAULT_DIGEST_INTERVAL_SEC,
        state_path: Path | None = None,
    ) -> None:
        self._channels = channels
        self._threshold = max(1, failure_threshold)
        self._cooldown = failure_cooldown_sec
        self._digest_interval = digest_interval_sec
        self._state_path = state_path
        self._state = _HealthState()
        self._load()

    # ------------------------------------------------------------------

    def broadcast(self, subject: str, body: str) -> None:
        """Send to every channel. No dedupe — callers decide what is worth sending."""
        for ch in self._channels:
            try:
                ch.send(subject, body)
            except Exception as exc:
                logger.warning("Channel %s failed: %s", ch.name, exc)

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    def should_alert_failure(self, consecutive_failures: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if consecutive_failures < self._threshold:
            return False
        if self._state.alerted_streak == 0:
            return True
        return now - self._state.last_failure_alert_at >= self._cooldown

    def report_failure(
        self, consecutive_failures: int, last_error: str, *, now: float | None = None
    ) -> bool:
        """Alert if the streak warrants it. Returns whether a message was sent."""
        now = time.time() if now is None else now
        if not self.should_alert_failure(consecutive_failures, now=now):
            return False

        self.broadcast(
            f"SCANNER DOWN — {consecutive_failures} consecutive failures",
            f"The scan loop has failed {consecutive_failures} times in a row.\n"
            f"Last error: {last_error}\n\n"
            "It is still retrying with exponential backoff. Check:\n"
            "  python main.py status\n"
            "  tail -50 data/scanner.log",
        )
        self._state.last_failure_alert_at = now
        self._state.alerted_streak = consecutive_failures
        self._save()
        return True

    def report_recovery(self, failed_cycles: int, *, now: float | None = None) -> bool:
        """
        Report the end of an outage — but only one we actually announced.

        Reporting a recovery from an outage that was never alerted would be
        the first you heard of it, which is noise rather than information.
        """
        now = time.time() if now is None else now
        if self._state.alerted_streak == 0:
            self._state.alerted_streak = 0
            return False

        self.broadcast(
            "Scanner recovered",
            f"The scan loop is working again after {failed_cycles} failed cycles.",
        )
        self._state.alerted_streak = 0
        self._save()
        return True

    # ------------------------------------------------------------------
    # Digest
    # ------------------------------------------------------------------

    def digest_due(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self._state.last_digest_at is None:
            # First run: start the clock rather than sending immediately, so a
            # restart does not produce a digest every time the process starts.
            self._state.last_digest_at = now
            self._save()
            return False
        return now - self._state.last_digest_at >= self._digest_interval

    def send_digest(self, body: str, *, subject: str = "Scanner digest",
                    now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.broadcast(subject, body)
        self._state.last_digest_at = now
        self._save()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
            stored_digest = raw.get("last_digest_at")
            self._state = _HealthState(
                last_failure_alert_at=float(raw.get("last_failure_alert_at", 0.0)),
                alerted_streak=int(raw.get("alerted_streak", 0)),
                last_digest_at=None if stored_digest is None else float(stored_digest),
            )
        except Exception as exc:
            logger.warning("Could not read health state (%s); starting fresh", exc)
            self._state = _HealthState()

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "last_failure_alert_at": self._state.last_failure_alert_at,
                "alerted_streak": self._state.alerted_streak,
                "last_digest_at": self._state.last_digest_at,
            }, indent=2))
            tmp.replace(self._state_path)
        except Exception as exc:
            logger.warning("Could not persist health state: %s", exc)
