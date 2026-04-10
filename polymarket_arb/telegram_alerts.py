"""
Telegram notification service.

Sends alerts for:
  - Every trade entry and exit
  - Drawdown threshold breaches
  - Kill-switch activation
  - Daily P&L summaries
  - Bot start/stop events

Uses the Bot HTTP API directly via httpx to avoid heavy library dependencies.
Messages are queued and sent asynchronously to avoid blocking the main loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_DRAWDOWN_ALERT_PCT,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_QUEUE = 100          # drop oldest alerts if queue is full
_SEND_COOLDOWN = 0.35     # seconds between sends (Telegram rate limit ~30/s)
_MAX_RETRIES   = 3


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class TelegramAlerter:
    """
    Async Telegram message dispatcher.

    Enqueue messages with the `send_*` methods; the internal worker task
    drains the queue respecting rate limits.
    """

    def __init__(self) -> None:
        self._token   = TELEGRAM_BOT_TOKEN
        self._chat_id = TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_drawdown_alert: float = 0.0

        if not self._enabled:
            logger.warning(
                "Telegram alerts disabled – TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._enabled:
            self._task = asyncio.create_task(self._worker())
            logger.info("Telegram alert worker started.")

    async def stop(self) -> None:
        if self._task:
            # Drain the queue before stopping
            try:
                await asyncio.wait_for(self._queue.join(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # High-level alert builders
    # ------------------------------------------------------------------

    def send_trade_entry(
        self,
        *,
        mode: str,
        contract_key: str,
        side: str,
        size_usdc: float,
        entry_price: float,
        edge_pct: float,
        confidence: float,
        cex_implied: float,
        kelly_fraction: float,
        equity: float,
    ) -> None:
        mode_emoji = "📄" if mode == "paper" else "💸"
        msg = (
            f"{mode_emoji} *Trade Entry* [{mode.upper()}]\n"
            f"`{_ts()}`\n\n"
            f"Contract : `{contract_key}`\n"
            f"Side      : `{side}`\n"
            f"Size      : `{size_usdc:.2f} USDC`\n"
            f"Fill price: `{entry_price:.4f}`\n"
            f"CEX prob  : `{cex_implied:.1%}`\n"
            f"Edge      : `{edge_pct:.2f}%`\n"
            f"Confidence: `{confidence:.1f}%`\n"
            f"Kelly f   : `{kelly_fraction:.3f}`\n"
            f"Portfolio : `{equity:.2f} USDC`"
        )
        self._enqueue(msg)

    def send_trade_exit(
        self,
        *,
        trade_id: int,
        mode: str,
        contract_key: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl_usdc: float,
        equity: float,
    ) -> None:
        pnl_emoji = "✅" if pnl_usdc >= 0 else "❌"
        msg = (
            f"{pnl_emoji} *Trade Exit* [{mode.upper()}] #{trade_id}\n"
            f"`{_ts()}`\n\n"
            f"Contract : `{contract_key}`\n"
            f"Side      : `{side}`\n"
            f"Entry     : `{entry_price:.4f}`\n"
            f"Exit      : `{exit_price:.4f}`\n"
            f"P&L       : `{pnl_usdc:+.2f} USDC`\n"
            f"Portfolio : `{equity:.2f} USDC`"
        )
        self._enqueue(msg)

    def send_drawdown_alert(
        self,
        *,
        drawdown_pct: float,
        current_equity: float,
        peak_equity: float,
    ) -> None:
        # Throttle: at most one drawdown alert per 5 minutes
        now = time.time()
        if (now - self._last_drawdown_alert) < 300:
            return
        self._last_drawdown_alert = now

        severity = "⚠️" if drawdown_pct < 0.15 else "🚨"
        msg = (
            f"{severity} *Drawdown Alert*\n"
            f"`{_ts()}`\n\n"
            f"Drawdown  : `{drawdown_pct:.1%}`\n"
            f"Current   : `{current_equity:.2f} USDC`\n"
            f"Peak      : `{peak_equity:.2f} USDC`"
        )
        self._enqueue(msg)

    def send_kill_switch(
        self,
        *,
        reason: str,
        drawdown_pct: float,
        equity: float,
    ) -> None:
        msg = (
            f"🛑 *KILL SWITCH ACTIVATED*\n"
            f"`{_ts()}`\n\n"
            f"Reason    : `{reason}`\n"
            f"Drawdown  : `{drawdown_pct:.1%}`\n"
            f"Equity    : `{equity:.2f} USDC`\n\n"
            f"_Bot has halted all trading._"
        )
        self._enqueue(msg)

    def send_daily_summary(
        self,
        *,
        equity: float,
        daily_pnl: float,
        win_rate: float,
        total_trades: int,
        open_positions: int,
    ) -> None:
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        msg = (
            f"{pnl_emoji} *Daily Summary*\n"
            f"`{_ts()}`\n\n"
            f"Equity    : `{equity:.2f} USDC`\n"
            f"Daily P&L : `{daily_pnl:+.2f} USDC`\n"
            f"Win rate  : `{win_rate:.1f}%` ({total_trades} closed)\n"
            f"Open pos  : `{open_positions}`"
        )
        self._enqueue(msg)

    def send_bot_started(self, mode: str, equity: float) -> None:
        msg = (
            f"🤖 *Polymarket Arb Bot Started*\n"
            f"`{_ts()}`\n\n"
            f"Mode      : `{mode.upper()}`\n"
            f"Equity    : `{equity:.2f} USDC`"
        )
        self._enqueue(msg)

    def send_bot_stopped(self, reason: str, equity: float) -> None:
        msg = (
            f"🔴 *Bot Stopped*\n"
            f"`{_ts()}`\n\n"
            f"Reason    : `{reason}`\n"
            f"Final eq  : `{equity:.2f} USDC`"
        )
        self._enqueue(msg)

    def send_raw(self, text: str) -> None:
        self._enqueue(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enqueue(self, msg: str) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("Telegram queue full – dropping oldest message.")
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def _worker(self) -> None:
        """Drain the queue and send messages with rate limiting."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    msg = await self._queue.get()
                    await self._send(client, msg)
                    self._queue.task_done()
                    await asyncio.sleep(_SEND_COOLDOWN)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.error("Telegram worker error: %s", exc)
                    await asyncio.sleep(1.0)

    async def _send(self, client: httpx.AsyncClient, text: str) -> None:
        url = _API_BASE.format(token=self._token)
        payload: dict[str, Any] = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return
                # 429 rate-limited
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning("Telegram rate-limited – waiting %ds", retry_after)
                    await asyncio.sleep(retry_after)
                else:
                    logger.warning(
                        "Telegram send failed (HTTP %d): %s",
                        resp.status_code, resp.text[:200],
                    )
                    return
            except httpx.RequestError as exc:
                if attempt == _MAX_RETRIES:
                    logger.error("Telegram request error after %d attempts: %s", attempt, exc)
                else:
                    await asyncio.sleep(2 ** attempt)
