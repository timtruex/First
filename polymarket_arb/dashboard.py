"""
Rich terminal dashboard.

Layout (updated every ~1 second via Live):
┌─────────────────────────────────────────────────────────────────┐
│  POLYMARKET LATENCY ARB BOT  │  Mode: PAPER  │  2024-01-15 …   │
├──────────────┬──────────────┬──────────────┬────────────────────┤
│  Portfolio   │  Today P&L   │  Win Rate    │  Open Positions    │
│  1 024.50    │  +14.32      │  68.4% (26)  │  2                 │
├──────────────┴──────────────┴──────────────┴────────────────────┤
│  CEX Prices  │  BTC  98 432 │  ETH  3 215                       │
│  Signals     │  BTC_5M_UP  lag=4.2%  edge=2.2%  conf=89%        │
├─────────────────────────────────────────────────────────────────┤
│  Last 10 Trades                                                  │
│  #  │ Contract    │ Side │ Size  │ Entry  │ Exit   │ P&L        │
│  …                                                               │
├─────────────────────────────────────────────────────────────────┤
│  Open Positions                                                  │
│  …                                                               │
└─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import is_live_trading
from database import Database

if TYPE_CHECKING:
    from arbitrage_engine import ArbitrageSignal
    from binance_feed import BinanceFeed
    from trader import Position, Trader

logger = logging.getLogger(__name__)

_REFRESH_HZ = 1          # terminal refresh rate


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Dashboard:
    """
    Renders a live Rich terminal dashboard.

    Call update() from the main loop to push fresh data; the internal
    Live context handles re-rendering.
    """

    def __init__(
        self,
        db: "Database",
        trader: "Trader",
        binance: "BinanceFeed",
        get_signals: Callable[[], list["ArbitrageSignal"]],
    ) -> None:
        self._db          = db
        self._trader      = trader
        self._binance     = binance
        self._get_signals = get_signals

        self._console = Console()
        self._live    = Live(
            self._render(),
            console=self._console,
            refresh_per_second=_REFRESH_HZ,
            screen=True,
        )
        self._kill_switch_active = False
        self._kill_switch_reason = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._live.start()

    def stop(self) -> None:
        try:
            self._live.stop()
        except Exception:
            pass

    def update(self) -> None:
        """Push fresh render to the terminal."""
        try:
            self._live.update(self._render())
        except Exception as exc:
            logger.debug("Dashboard render error: %s", exc)

    def set_kill_switch(self, reason: str) -> None:
        self._kill_switch_active = True
        self._kill_switch_reason = reason

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header",   size=3),
            Layout(name="stats",    size=5),
            Layout(name="prices",   size=4),
            Layout(name="signals",  size=6),
            Layout(name="trades",   size=14),
            Layout(name="positions",size=8),
            Layout(name="footer",   size=3),
        )

        layout["header"].update(self._header_panel())
        layout["stats"].update(self._stats_panel())
        layout["prices"].update(self._prices_panel())
        layout["signals"].update(self._signals_panel())
        layout["trades"].update(self._trades_panel())
        layout["positions"].update(self._positions_panel())
        layout["footer"].update(self._footer_panel())

        return layout

    # ------------------------------------------------------------------
    # Individual panels
    # ------------------------------------------------------------------

    def _header_panel(self) -> Panel:
        mode      = "LIVE" if is_live_trading() else "PAPER"
        mode_style= "bold red" if is_live_trading() else "bold yellow"
        ks_badge  = " [bold red] KILL SWITCH ACTIVE [/bold red]" if self._kill_switch_active else ""
        title     = (
            f"[bold cyan]POLYMARKET LATENCY ARB BOT[/bold cyan]  "
            f"Mode: [{mode_style}]{mode}[/{mode_style}]{ks_badge}  "
            f"[dim]{_now_str()}[/dim]"
        )
        return Panel(Align.center(title), style="bright_blue")

    def _stats_panel(self) -> Panel:
        equity     = self._trader.equity()
        daily_pnl  = self._db.get_daily_pnl()
        wins, total, win_rate = self._db.get_win_rate()
        open_count = len(self._trader.get_open_positions())

        pnl_style = "green" if daily_pnl >= 0 else "red"
        pnl_sign  = "+" if daily_pnl >= 0 else ""

        stats = [
            Panel(
                f"[bold white]{equity:.2f}[/bold white]\n[dim]USDC Equity[/dim]",
                title="[cyan]Portfolio[/cyan]", border_style="cyan",
            ),
            Panel(
                f"[bold {pnl_style}]{pnl_sign}{daily_pnl:.2f}[/bold {pnl_style}]\n[dim]USDC Today[/dim]",
                title="[cyan]Daily P&L[/cyan]", border_style="cyan",
            ),
            Panel(
                f"[bold white]{win_rate:.1f}%[/bold white]\n[dim]{wins}/{total} trades[/dim]",
                title="[cyan]Win Rate[/cyan]", border_style="cyan",
            ),
            Panel(
                f"[bold white]{open_count}[/bold white]\n[dim]positions[/dim]",
                title="[cyan]Open Positions[/cyan]", border_style="cyan",
            ),
        ]
        return Panel(Columns(stats, equal=True, expand=True), title="[bold]Statistics[/bold]")

    def _prices_panel(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column("Asset", style="bold cyan", width=8)
        table.add_column("Price", style="white", width=12)
        table.add_column("5m Prob Up", width=14)
        table.add_column("15m Prob Up", width=14)
        table.add_column("Stale?", width=8)

        for sym_lower, label in (("btcusdt", "BTC"), ("ethusdt", "ETH")):
            state = self._binance.get_state(sym_lower)
            if state:
                price_str = f"{state.latest_price:,.2f}" if state.latest_price else "—"
                p5  = state.implied_up_prob(5)
                p15 = state.implied_up_prob(15)
                p5_str  = f"{p5:.1%}"  if p5  is not None else "—"
                p15_str = f"{p15:.1%}" if p15 is not None else "—"
                stale   = "[red]YES[/red]" if state.is_stale() else "[green]no[/green]"
            else:
                price_str = p5_str = p15_str = "—"
                stale = "[red]YES[/red]"

            table.add_row(label, price_str, p5_str, p15_str, stale)

        return Panel(table, title="[bold]CEX Prices & Implied Probabilities[/bold]")

    def _signals_panel(self) -> Panel:
        signals = self._get_signals()
        if not signals:
            return Panel("[dim]No active signals[/dim]", title="[bold]Recent Signals[/bold]")

        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Contract",   style="cyan",  width=16)
        table.add_column("Poly Price", width=10)
        table.add_column("CEX Prob",   width=10)
        table.add_column("Lag %",      width=8)
        table.add_column("Edge %",     width=8)
        table.add_column("Conf %",     width=8)
        table.add_column("Side",       width=6)
        table.add_column("Size",       width=10)
        table.add_column("Status",     width=12)

        for sig in signals[:8]:  # show at most 8
            status_style = "green" if sig.is_actionable else "dim"
            status_text  = "ACTIONABLE" if sig.is_actionable else (sig.skip_reason or "SKIP")[:12]
            table.add_row(
                sig.contract_key,
                f"{sig.poly_price:.4f}",
                f"{sig.cex_implied_prob:.4f}",
                f"[yellow]{sig.lag_pct:.2f}[/yellow]",
                f"[cyan]{sig.edge_pct:.2f}[/cyan]",
                f"{sig.confidence:.1f}",
                sig.recommended_side,
                f"{sig.kelly_size_usdc:.2f}",
                f"[{status_style}]{status_text}[/{status_style}]",
            )

        return Panel(table, title="[bold]Signal Scanner[/bold]")

    def _trades_panel(self) -> Panel:
        rows = self._db.get_recent_trades(limit=10)
        if not rows:
            return Panel("[dim]No trades yet[/dim]", title="[bold]Last 10 Trades[/bold]")

        table = Table(show_header=True, header_style="bold blue", expand=True)
        table.add_column("#",         width=5)
        table.add_column("Time",      width=20)
        table.add_column("Contract",  width=16)
        table.add_column("Side",      width=6)
        table.add_column("Mode",      width=6)
        table.add_column("Size",      width=10)
        table.add_column("Entry",     width=8)
        table.add_column("Exit",      width=8)
        table.add_column("P&L",       width=10)
        table.add_column("Status",    width=8)

        for raw in rows:
            # Stored rows are integer minor units; decode_trade is the single
            # place that knows the encoding.
            row = Database.decode_trade(raw)
            pnl_val  = row["pnl_usdc"]
            pnl_str  = f"{pnl_val:+.2f}" if pnl_val is not None else "—"
            pnl_style= "green" if (pnl_val is None or pnl_val >= 0) else "red"
            exit_str = f"{row['exit_price']:.4f}" if row["exit_price"] is not None else "—"
            ts_short = (row["ts"] or "")[:19]

            table.add_row(
                str(row["id"]),
                ts_short,
                row["contract_key"],
                row["side"],
                str(row["mode"])[:4].upper(),
                f"{row['cost_usdc']:.2f}",
                f"{row['entry_price']:.4f}",
                exit_str,
                f"[{pnl_style}]{pnl_str}[/{pnl_style}]",
                row["status"],
            )

        return Panel(table, title="[bold]Last 10 Trades[/bold]")

    def _positions_panel(self) -> Panel:
        positions = self._trader.get_open_positions()
        if not positions:
            return Panel("[dim]No open positions[/dim]", title="[bold]Open Positions[/bold]")

        table = Table(show_header=True, header_style="bold green", expand=True)
        table.add_column("#",         width=6)
        table.add_column("Contract",  width=16)
        table.add_column("Side",      width=6)
        table.add_column("Size",      width=10)
        table.add_column("Entry",     width=8)
        table.add_column("Age",       width=12)

        import time
        now = time.time()
        for pos in positions:
            age_sec  = int(now - pos.opened_at)
            age_str  = f"{age_sec // 60}m {age_sec % 60}s"
            table.add_row(
                str(pos.trade_id),
                pos.contract_key,
                pos.side,
                f"{pos.cost_usdc:.2f}",
                f"{pos.entry_price:.4f}",
                age_str,
            )

        return Panel(table, title="[bold]Open Positions[/bold]")

    def _footer_panel(self) -> Panel:
        binance_health = "[green]OK[/green]" if self._binance.is_healthy() else "[red]STALE[/red]"
        ks_note = (
            f"  [red]KILL SWITCH: {self._kill_switch_reason}[/red]"
            if self._kill_switch_active else ""
        )
        text = (
            f"Binance feed: {binance_health}  "
            f"| Press [bold]Ctrl+C[/bold] to exit{ks_note}"
        )
        return Panel(Align.center(text), style="dim")
