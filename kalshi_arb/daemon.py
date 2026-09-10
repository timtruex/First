"""
Long-running scan loop for continuous local operation.

The one-shot `scan` command is fine for a person at a terminal. Running for
weeks unattended needs different properties, and each of these exists because
of a specific way an unattended loop fails:

  graceful shutdown   launchd sends SIGTERM and SIGKILLs after a grace period.
                      A loop that ignores SIGTERM gets killed mid-request and
                      loses in-flight state; this finishes the cycle and exits.

  backoff on failure  Both venues go down. Retrying every 30s through an
                      outage burns the rate-limit budget and buries the real
                      error in thousands of identical log lines. Consecutive
                      failures back off exponentially to a cap and reset on
                      the first success.

  heartbeat file      A process can be alive and wedged. The heartbeat records
                      the last *completed* cycle, so liveness is observable
                      from outside without parsing logs.

  bounded logs        Weeks of 30-second cycles fill a disk. Rotation is set
                      up here rather than left to the operator.

  alert suppression   see notify.py — the reason a 24/7 scanner stays useful.

  outage + digest     spread alerts are silent when there is nothing to
                      report, so silence carries no information on its own: a
                      scanner finding nothing looks exactly like one that died
                      on Tuesday. Failure alerts and a periodic digest close
                      that from both directions.

The loop never places orders. It is a monitor: it finds spreads and tells you.
Execution is a separate decision with a separate risk profile, and wiring it
into an unattended loop is not something to do implicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from money import ZERO, fmt_usd
from scanner import Spread

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
DEFAULT_MAX_BACKOFF_SEC = 900.0


def setup_rotating_logs(path: Path, level: str = "INFO", *, max_bytes: int = 10_000_000,
                        backups: int = 5) -> None:
    """Console plus a size-capped rotating file. 10MB x 5 bounds disk at ~50MB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s – %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups
    )
    file_handler.setFormatter(fmt)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream)


def backoff_delay(consecutive_failures: int, base: float, cap: float) -> float:
    """
    Exponential backoff, capped.

    Deterministic rather than jittered: this is a single local process, not a
    fleet, so there is no thundering herd to spread out, and a predictable
    retry schedule is easier to reason about in a log.
    """
    if consecutive_failures <= 0:
        return base
    return min(base * (2 ** consecutive_failures), cap)


@dataclass
class DaemonStats:
    started_at: float = field(default_factory=time.time)
    cycles: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    spreads_found: int = 0
    alerts_sent: int = 0
    last_cycle_at: float | None = None
    last_error: str | None = None

    # Reset each time a digest is sent, so the digest describes its own period
    # rather than all time — "3 spreads since I started six weeks ago" answers
    # a question nobody asked.
    period_started_at: float = field(default_factory=time.time)
    period_cycles: int = 0
    period_failures: int = 0
    period_spreads: int = 0
    period_alerts: int = 0
    period_best_edge: Decimal = ZERO
    period_best_pair: str = ""

    def note_spreads(self, spreads: list[Spread]) -> None:
        self.spreads_found += len(spreads)
        self.period_spreads += len(spreads)
        for s in spreads:
            if s.net_edge_per_contract > self.period_best_edge:
                self.period_best_edge = s.net_edge_per_contract
                self.period_best_pair = s.pair_id

    def reset_period(self, *, now: float | None = None) -> None:
        self.period_started_at = time.time() if now is None else now
        self.period_cycles = 0
        self.period_failures = 0
        self.period_spreads = 0
        self.period_alerts = 0
        self.period_best_edge = ZERO
        self.period_best_pair = ""

    def digest_body(self) -> str:
        hours = (time.time() - self.period_started_at) / 3600
        healthy = self.consecutive_failures == 0
        lines = [
            f"Period          {hours:.1f}h",
            f"Cycles          {self.period_cycles}",
            f"Failures        {self.period_failures}",
            f"Spreads found   {self.period_spreads}",
            f"Alerts sent     {self.period_alerts}",
        ]
        if self.period_best_edge > ZERO:
            lines.append(
                f"Best edge       {fmt_usd(self.period_best_edge, 4)}/contract "
                f"({self.period_best_pair})"
            )
        else:
            lines.append("Best edge       none above threshold")
        lines.append("")
        lines.append(
            "Status          healthy" if healthy
            else f"Status          FAILING ({self.consecutive_failures} consecutive)"
        )
        if self.last_error:
            lines.append(f"Last error      {self.last_error}")
        lines.append(f"Uptime          {(time.time() - self.started_at) / 3600:.1f}h")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        def iso(ts: float | None) -> str | None:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
        return {
            "started_at": iso(self.started_at),
            "last_cycle_at": iso(self.last_cycle_at),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "cycles": self.cycles,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "spreads_found": self.spreads_found,
            "alerts_sent": self.alerts_sent,
            "last_error": self.last_error,
            "period_cycles": self.period_cycles,
            "period_failures": self.period_failures,
            "period_spreads": self.period_spreads,
            "period_best_edge": str(self.period_best_edge),
        }


class ScanDaemon:
    """
    Supervises a scan callable on an interval.

    `scan_once` is injected rather than imported so the loop's behaviour —
    backoff, shutdown, heartbeat — is testable without touching a network.
    """

    def __init__(
        self,
        scan_once: Callable[[], Awaitable[list[Spread]]],
        *,
        on_spreads: Callable[[list[Spread]], int] | None = None,
        on_failure: Callable[[int, str], None] | None = None,
        on_recovery: Callable[[int], None] | None = None,
        digest_due: Callable[[], bool] | None = None,
        on_digest: Callable[[str], None] | None = None,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        max_backoff_sec: float = DEFAULT_MAX_BACKOFF_SEC,
        heartbeat_path: Path | None = None,
        max_cycles: int | None = None,
    ) -> None:
        self._scan_once = scan_once
        self._on_spreads = on_spreads
        self._on_failure = on_failure
        self._on_recovery = on_recovery
        self._digest_due = digest_due
        self._on_digest = on_digest
        self._interval = interval_sec
        self._max_backoff = max_backoff_sec
        self._heartbeat_path = heartbeat_path
        self._max_cycles = max_cycles
        self._stop = asyncio.Event()
        self.stats = DaemonStats()

    # ------------------------------------------------------------------

    def request_stop(self, reason: str = "signal") -> None:
        if not self._stop.is_set():
            logger.info("Shutdown requested (%s); finishing current cycle…", reason)
            self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop, sig.name)
            except NotImplementedError:  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: self.request_stop("signal"))

    def _write_heartbeat(self) -> None:
        if not self._heartbeat_path:
            return
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._heartbeat_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.stats.as_dict(), indent=2))
            tmp.replace(self._heartbeat_path)
        except Exception as exc:
            # Never let observability take down the thing being observed.
            logger.warning("Could not write heartbeat: %s", exc)

    # ------------------------------------------------------------------

    async def _run_cycle(self) -> float:
        """Run one scan. Returns the delay before the next cycle."""
        try:
            spreads = await self._scan_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.failures += 1
            self.stats.period_failures += 1
            self.stats.consecutive_failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            delay = backoff_delay(self.stats.consecutive_failures, self._interval, self._max_backoff)
            logger.error(
                "Scan cycle failed (%d consecutive): %s — retrying in %.0fs",
                self.stats.consecutive_failures, exc, delay,
            )
            if self._on_failure is not None:
                try:
                    self._on_failure(self.stats.consecutive_failures, self.stats.last_error)
                except Exception as alert_exc:
                    # A broken alert channel must not convert a recoverable
                    # scan failure into a crashed daemon.
                    logger.warning("Failure alert dispatch failed: %s", alert_exc)
            self._write_heartbeat()
            return delay

        if self.stats.consecutive_failures:
            failed = self.stats.consecutive_failures
            logger.info("Recovered after %d consecutive failures", failed)
            if self._on_recovery is not None:
                try:
                    self._on_recovery(failed)
                except Exception as exc:
                    logger.warning("Recovery alert dispatch failed: %s", exc)
        self.stats.consecutive_failures = 0
        self.stats.last_error = None
        self.stats.cycles += 1
        self.stats.period_cycles += 1
        self.stats.last_cycle_at = time.time()
        self.stats.note_spreads(spreads)

        if spreads and self._on_spreads is not None:
            try:
                sent = self._on_spreads(spreads)
                self.stats.alerts_sent += sent
                self.stats.period_alerts += sent
            except Exception as exc:
                logger.warning("Alert dispatch failed: %s", exc)

        self._maybe_digest()

        logger.info(
            "Cycle %d complete: %d spreads (%d alerts total, %d failures)",
            self.stats.cycles, len(spreads), self.stats.alerts_sent, self.stats.failures,
        )
        self._write_heartbeat()
        return self._interval

    def _maybe_digest(self) -> None:
        """
        Emit the periodic digest when due.

        Sent unconditionally when the interval elapses, including when nothing
        happened — a digest that only reports interesting news would reinstate
        exactly the ambiguity it exists to remove.
        """
        if self._digest_due is None or self._on_digest is None:
            return
        try:
            if not self._digest_due():
                return
            self._on_digest(self.stats.digest_body())
            self.stats.reset_period()
        except Exception as exc:
            logger.warning("Digest dispatch failed: %s", exc)

    async def run(self) -> DaemonStats:
        logger.info(
            "Scan daemon starting: interval=%gs max_backoff=%gs",
            self._interval, self._max_backoff,
        )
        self._write_heartbeat()

        while not self._stop.is_set():
            delay = await self._run_cycle()

            if self._max_cycles is not None and self.stats.cycles >= self._max_cycles:
                logger.info("Reached max_cycles=%d; stopping", self._max_cycles)
                break

            # Waiting on the stop event rather than sleeping means SIGTERM is
            # honoured immediately instead of after a full interval — which is
            # the difference between a clean exit and launchd SIGKILLing us.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

        logger.info(
            "Scan daemon stopped after %d cycles, %d failures, %d alerts",
            self.stats.cycles, self.stats.failures, self.stats.alerts_sent,
        )
        self._write_heartbeat()
        return self.stats
