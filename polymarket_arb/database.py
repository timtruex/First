"""
SQLite persistence layer.

Schema
------
trades          – every executed (paper or live) order
signals         – every arbitrage signal that was detected (traded or not)
portfolio_snapshots – periodic equity snapshots for P&L charting
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Generator

from config import DB_PATH

logger = logging.getLogger(__name__)

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,           -- ISO-8601 UTC
    mode            TEXT    NOT NULL,           -- 'paper' | 'live'
    contract_key    TEXT    NOT NULL,           -- e.g. 'BTC_5M_UP'
    token_id        TEXT    NOT NULL,
    side            TEXT    NOT NULL,           -- 'BUY' | 'SELL'
    size_usdc       REAL    NOT NULL,
    entry_price     REAL    NOT NULL,           -- Polymarket fill price (0-1)
    cex_implied_prob REAL   NOT NULL,           -- CEX-derived probability at signal time
    edge_pct        REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    kelly_fraction  REAL    NOT NULL,
    order_id        TEXT,                       -- NULL for paper trades
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | CANCELLED
    exit_price      REAL,
    pnl_usdc        REAL,
    closed_ts       TEXT
);
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    contract_key    TEXT    NOT NULL,
    poly_price      REAL    NOT NULL,
    cex_implied_prob REAL   NOT NULL,
    lag_pct         REAL    NOT NULL,
    edge_pct        REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    acted           INTEGER NOT NULL DEFAULT 0,  -- 1 if a trade was placed
    skip_reason     TEXT                         -- why we skipped, if acted=0
);
"""

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    equity_usdc REAL    NOT NULL,
    open_positions INTEGER NOT NULL DEFAULT 0,
    daily_pnl   REAL    NOT NULL DEFAULT 0.0
);
"""

_CREATE_KILL_SWITCH_LOG = """
CREATE TABLE IF NOT EXISTS kill_switch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    equity_usdc REAL    NOT NULL
);
"""


class Database:
    """Thread-safe SQLite wrapper."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = sqlite3.connect(self._path, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                _CREATE_TRADES
                + _CREATE_SIGNALS
                + _CREATE_SNAPSHOTS
                + _CREATE_KILL_SWITCH_LOG
            )
        logger.debug("Database schema initialised at %s", self._path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def insert_trade(
        self,
        *,
        mode: str,
        contract_key: str,
        token_id: str,
        side: str,
        size_usdc: float,
        entry_price: float,
        cex_implied_prob: float,
        edge_pct: float,
        confidence: float,
        kelly_fraction: float,
        order_id: str | None = None,
    ) -> int:
        sql = """
        INSERT INTO trades
            (ts, mode, contract_key, token_id, side, size_usdc, entry_price,
             cex_implied_prob, edge_pct, confidence, kelly_fraction, order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """
        with self._conn() as conn:
            cur = conn.execute(
                sql,
                (
                    self._now(), mode, contract_key, token_id, side,
                    size_usdc, entry_price, cex_implied_prob,
                    edge_pct, confidence, kelly_fraction, order_id,
                ),
            )
            trade_id = cur.lastrowid
        logger.info("Trade #%d inserted (%s %s %.4f USDC)", trade_id, side, contract_key, size_usdc)
        return trade_id  # type: ignore[return-value]

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl_usdc: float,
    ) -> None:
        sql = """
        UPDATE trades
        SET status='CLOSED', exit_price=?, pnl_usdc=?, closed_ts=?
        WHERE id=?
        """
        with self._conn() as conn:
            conn.execute(sql, (exit_price, pnl_usdc, self._now(), trade_id))
        logger.info("Trade #%d closed – PnL %.4f USDC", trade_id, pnl_usdc)

    def get_open_trades(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY ts DESC"
            ).fetchall()

    def get_recent_trades(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_daily_pnl(self) -> float:
        """Sum of realised PnL for closed trades today (UTC)."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc), 0.0) FROM trades "
                "WHERE status='CLOSED' AND closed_ts >= ?",
                (today,),
            ).fetchone()
        return float(row[0])

    def get_win_rate(self) -> tuple[int, int, float]:
        """Return (wins, total_closed, win_rate_pct)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE status='CLOSED'"
            ).fetchone()
        total = row["total"] or 0
        wins  = row["wins"]  or 0
        rate  = (wins / total * 100) if total else 0.0
        return wins, total, rate

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def insert_signal(
        self,
        *,
        contract_key: str,
        poly_price: float,
        cex_implied_prob: float,
        lag_pct: float,
        edge_pct: float,
        confidence: float,
        acted: bool = False,
        skip_reason: str | None = None,
    ) -> int:
        sql = """
        INSERT INTO signals
            (ts, contract_key, poly_price, cex_implied_prob, lag_pct,
             edge_pct, confidence, acted, skip_reason)
        VALUES (?,?,?,?,?,?,?,?,?)
        """
        with self._conn() as conn:
            cur = conn.execute(
                sql,
                (
                    self._now(), contract_key, poly_price, cex_implied_prob,
                    lag_pct, edge_pct, confidence, int(acted), skip_reason,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def insert_snapshot(
        self,
        equity_usdc: float,
        open_positions: int,
        daily_pnl: float,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots (ts, equity_usdc, open_positions, daily_pnl) "
                "VALUES (?,?,?,?)",
                (self._now(), equity_usdc, open_positions, daily_pnl),
            )

    def get_peak_equity_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(equity_usdc), 0.0) FROM portfolio_snapshots WHERE ts >= ?",
                (today,),
            ).fetchone()
        return float(row[0])

    # ------------------------------------------------------------------
    # Kill-switch log
    # ------------------------------------------------------------------

    def log_kill_switch(self, reason: str, equity_usdc: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kill_switch_log (ts, reason, equity_usdc) VALUES (?,?,?)",
                (self._now(), reason, equity_usdc),
            )
        logger.critical("KILL SWITCH triggered: %s  equity=%.2f", reason, equity_usdc)
