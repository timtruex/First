"""
Entry point for the Polymarket latency arbitrage bot.

Startup sequence
----------------
1. Validate configuration (warn about missing token IDs, credentials).
2. Initialise all sub-systems (DB, feeds, engine, trader, alerts, dashboard).
3. Start the Binance WebSocket feed and Polymarket CLOB poller.
4. Run the main arbitrage loop:
   a. Scan for signals.
   b. For each actionable signal, enter a position.
   c. Monitor open positions for exit conditions.
   d. Check kill-switch threshold.
   e. Update dashboard.
5. Handle graceful shutdown on SIGINT / SIGTERM.

Usage
-----
  python main.py                  # paper trading (default)
  python main.py --check-config   # validate env and exit
  python main.py --log-level DEBUG

Live trading requires env vars (see .env.example):
  PAPER_TRADING=false
  LIVE_CONFIRM_1=true
  LIVE_CONFIRM_2=true
  LIVE_CONFIRM_3=true
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Any

from config import (
    CONTRACT_TOKEN_IDS,
    LOG_LEVEL,
    LOG_PATH,
    MAX_DAILY_DRAWDOWN,
    STARTING_PORTFOLIO_USDC,
    TELEGRAM_DRAWDOWN_ALERT_PCT,
    is_live_trading,
)

# ---------------------------------------------------------------------------
# Logging setup (before any other imports that use loggers)
# ---------------------------------------------------------------------------

def _setup_logging(level: str = LOG_LEVEL) -> None:
    fmt = "%(asctime)s %(levelname)-8s %(name)s – %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot state
# ---------------------------------------------------------------------------

_SCAN_INTERVAL_SEC  = 1.0    # how often to run the signal scan
_SNAPSHOT_INTERVAL  = 60.0   # how often to write portfolio snapshot
_DAILY_SUMMARY_HOUR = 23     # UTC hour to send daily Telegram summary
_POSITION_MAX_AGE   = 14 * 60  # seconds – close stale positions (e.g. 14 min before contract expires)


class ArbBot:
    """Top-level orchestrator."""

    def __init__(self) -> None:
        from arbitrage_engine import ArbitrageEngine, is_kill_switch_triggered
        from binance_feed import BinanceFeed
        from dashboard import Dashboard
        from database import Database
        from polymarket_feed import PolymarketFeed
        from telegram_alerts import TelegramAlerter
        from trader import Trader

        self._db       = Database()
        self._binance  = BinanceFeed()
        self._poly     = PolymarketFeed()
        self._trader   = Trader(db=self._db)
        self._telegram = TelegramAlerter()
        self._is_ks_triggered = is_kill_switch_triggered  # function ref

        # Engine gets live callbacks into trader state
        self._engine = ArbitrageEngine(
            binance=self._binance,
            polymarket=self._poly,
            portfolio_equity_fn=self._trader.equity,
            open_position_value_fn=self._trader.open_position_value,
        )

        # Latest signal list is shared with the dashboard closure
        self._latest_signals: list[Any] = []

        self._dashboard = Dashboard(
            db=self._db,
            trader=self._trader,
            binance=self._binance,
            get_signals=lambda: self._latest_signals,
        )

        self._kill_switch_active = False
        self._running = False
        self._last_snapshot_ts: float = 0.0
        self._last_summary_hour: int = -1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        mode = "LIVE" if is_live_trading() else "PAPER"
        logger.info("Starting bot in %s mode.", mode)

        await self._telegram.start()
        await self._binance.start()
        await self._poly.start()
        self._dashboard.start()

        self._telegram.send_bot_started(mode, self._trader.equity())

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown("graceful stop")

    async def _shutdown(self, reason: str) -> None:
        logger.info("Shutting down: %s", reason)
        self._running = False

        await self._binance.stop()
        await self._poly.stop()
        self._dashboard.stop()

        self._telegram.send_bot_stopped(reason, self._trader.equity())
        await self._telegram.stop()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        while self._running:
            loop_start = time.monotonic()

            if not self._kill_switch_active:
                await self._scan_and_trade()
                self._check_position_exits()
                self._check_kill_switch()

            self._maybe_snapshot()
            self._maybe_daily_summary()
            self._dashboard.update()

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, _SCAN_INTERVAL_SEC - elapsed)
            await asyncio.sleep(sleep_time)

    # ------------------------------------------------------------------
    # Signal scan & trade entry
    # ------------------------------------------------------------------

    async def _scan_and_trade(self) -> None:
        signals = self._engine.scan()
        self._latest_signals = signals

        for sig in signals:
            if not sig.is_actionable:
                # Log every signal to DB regardless
                self._db.insert_signal(
                    contract_key=sig.contract_key,
                    poly_price=sig.poly_price,
                    cex_implied_prob=sig.cex_implied_prob,
                    lag_pct=sig.lag_pct,
                    edge_pct=sig.edge_pct,
                    confidence=sig.confidence,
                    acted=False,
                    skip_reason=sig.skip_reason,
                )
                continue

            # Check we haven't already got this contract open
            open_keys = {p.contract_key for p in self._trader.get_open_positions()}
            if sig.contract_key in open_keys:
                logger.debug("Already have open position for %s – skipping.", sig.contract_key)
                continue

            # Execute
            mode = "live" if is_live_trading() else "paper"
            from config import KELLY_FRACTION
            pos = await self._trader.enter(
                contract_key=sig.contract_key,
                token_id=sig.token_id,
                side=sig.recommended_side,
                size_usdc=sig.kelly_size_usdc,
                poly_price=sig.poly_price,
                cex_implied_prob=sig.cex_implied_prob,
                edge_pct=sig.edge_pct,
                confidence=sig.confidence,
                kelly_fraction=KELLY_FRACTION,
            )

            if pos:
                self._db.insert_signal(
                    contract_key=sig.contract_key,
                    poly_price=sig.poly_price,
                    cex_implied_prob=sig.cex_implied_prob,
                    lag_pct=sig.lag_pct,
                    edge_pct=sig.edge_pct,
                    confidence=sig.confidence,
                    acted=True,
                )
                self._telegram.send_trade_entry(
                    mode=mode,
                    contract_key=sig.contract_key,
                    side=sig.recommended_side,
                    size_usdc=sig.kelly_size_usdc,
                    entry_price=pos.entry_price,
                    edge_pct=sig.edge_pct,
                    confidence=sig.confidence,
                    cex_implied=sig.cex_implied_prob,
                    kelly_fraction=KELLY_FRACTION,
                    equity=self._trader.equity(),
                )

    # ------------------------------------------------------------------
    # Position exit logic
    # ------------------------------------------------------------------

    def _check_position_exits(self) -> None:
        """
        Simple exit strategy:
          - Close if the edge has reversed (Polymarket has caught up or overshot).
          - Close if position is older than _POSITION_MAX_AGE (contract nearing expiry).
        """
        now = time.time()
        mode = "live" if is_live_trading() else "paper"

        for pos in list(self._trader.get_open_positions()):
            current_mid = self._poly.get_mid(pos.token_id)
            should_close = False
            exit_price = pos.entry_price  # fallback

            # Age-based exit
            if (now - pos.opened_at) >= _POSITION_MAX_AGE:
                should_close = True
                if current_mid is not None:
                    exit_price = current_mid
                logger.info("Closing %s (age limit reached).", pos.contract_key)

            # Edge-reversal exit: if we bought and price has risen past entry + edge
            elif current_mid is not None:
                if pos.side == "BUY" and current_mid >= pos.entry_price * 1.03:
                    should_close = True
                    exit_price = current_mid
                elif pos.side == "SELL" and current_mid <= pos.entry_price * 0.97:
                    should_close = True
                    exit_price = current_mid

            if should_close:
                # Run close in background to not block the sync caller
                asyncio.ensure_future(self._close_position(pos, exit_price, mode))

    async def _close_position(
        self,
        pos: Any,
        exit_price: float,
        mode: str,
    ) -> None:
        pnl = await self._trader.close_position(pos.trade_id, exit_price)
        self._telegram.send_trade_exit(
            trade_id=pos.trade_id,
            mode=mode,
            contract_key=pos.contract_key,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_usdc=pnl,
            equity=self._trader.equity(),
        )

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> None:
        equity     = self._trader.equity()
        peak       = self._db.get_peak_equity_today()
        if peak == 0:
            peak = float(STARTING_PORTFOLIO_USDC)

        triggered, dd = self._is_ks_triggered(equity, peak)

        # Warn at drawdown alert threshold
        if dd >= TELEGRAM_DRAWDOWN_ALERT_PCT:
            self._telegram.send_drawdown_alert(
                drawdown_pct=dd,
                current_equity=equity,
                peak_equity=peak,
            )

        if triggered and not self._kill_switch_active:
            reason = f"Daily drawdown {dd:.1%} >= limit {MAX_DAILY_DRAWDOWN:.1%}"
            self._kill_switch_active = True
            self._dashboard.set_kill_switch(reason)
            self._db.log_kill_switch(reason, equity)
            self._telegram.send_kill_switch(
                reason=reason,
                drawdown_pct=dd,
                equity=equity,
            )
            logger.critical("KILL SWITCH: %s", reason)

    # ------------------------------------------------------------------
    # Periodic tasks
    # ------------------------------------------------------------------

    def _maybe_snapshot(self) -> None:
        now = time.time()
        if (now - self._last_snapshot_ts) >= _SNAPSHOT_INTERVAL:
            self._last_snapshot_ts = now
            self._db.insert_snapshot(
                equity_usdc=self._trader.equity(),
                open_positions=len(self._trader.get_open_positions()),
                daily_pnl=self._db.get_daily_pnl(),
            )

    def _maybe_daily_summary(self) -> None:
        import datetime
        now_hour = datetime.datetime.now(datetime.timezone.utc).hour
        if now_hour == _DAILY_SUMMARY_HOUR and now_hour != self._last_summary_hour:
            self._last_summary_hour = now_hour
            wins, total, win_rate = self._db.get_win_rate()
            self._telegram.send_daily_summary(
                equity=self._trader.equity(),
                daily_pnl=self._db.get_daily_pnl(),
                win_rate=win_rate,
                total_trades=total,
                open_positions=len(self._trader.get_open_positions()),
            )


# ---------------------------------------------------------------------------
# Config validation helper
# ---------------------------------------------------------------------------

def _check_config() -> bool:
    issues: list[str] = []

    missing_tokens = [k for k, v in CONTRACT_TOKEN_IDS.items() if not v]
    if missing_tokens:
        issues.append(f"Missing token IDs: {missing_tokens}")

    from config import POLY_PRIVATE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if not POLY_PRIVATE_KEY:
        issues.append("POLY_PRIVATE_KEY not set (read-only / paper mode only)")
    if not TELEGRAM_BOT_TOKEN:
        issues.append("TELEGRAM_BOT_TOKEN not set (no alerts)")
    if not TELEGRAM_CHAT_ID:
        issues.append("TELEGRAM_CHAT_ID not set (no alerts)")

    if is_live_trading():
        print("Mode: LIVE TRADING ACTIVE")
    else:
        print("Mode: PAPER TRADING")

    if issues:
        print("\nConfiguration warnings:")
        for i in issues:
            print(f"  ⚠  {i}")
        return False
    print("\nConfiguration looks good.")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket latency arbitrage bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="Validate configuration and exit",
    )
    parser.add_argument(
        "--log-level", default=LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: %(default)s)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)

    if args.check_config:
        ok = _check_config()
        sys.exit(0 if ok else 1)

    bot = ArbBot()

    # Install signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal() -> None:
        logger.info("Shutdown signal received.")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        logger.info("Bot exited cleanly.")


if __name__ == "__main__":
    main()
