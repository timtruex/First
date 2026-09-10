"""
SQLite persistence layer.

Schema
------
trades          – every executed (paper or live) order
signals         – every arbitrage signal that was detected (traded or not)
portfolio_snapshots – periodic equity snapshots for P&L charting

Numeric policy
--------------
Money and price columns are INTEGER minor units (micro-USDC at 1e-6,
centi-cents at 1e-4), not REAL.  SQLite's REAL affinity is a C double, so
storing a Decimal in one round-trips it back as a float and silently undoes
the exact arithmetic upstream — and `SUM(pnl)` over a trading day then
accumulates that error into the figure the drawdown kill-switch reads.
Integers are exact under SQLite arithmetic including SUM() and MAX().

Dimensionless statistics (probabilities, edge %, confidence, Kelly fraction)
stay REAL: they never enter a balance, and float is the right type for them.
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
from money import (
    ZERO,
    centi_to_price,
    fmt_price,
    fmt_usdc,
    micro_to_shares,
    micro_to_usdc,
    price_to_centi,
    shares_to_micro,
    usdc_to_micro,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,           -- ISO-8601 UTC
    mode            TEXT    NOT NULL,           -- 'paper' | 'live'
    contract_key    TEXT    NOT NULL,           -- e.g. 'BTC_5M_UP'
    token_id        TEXT    NOT NULL,
    side            TEXT    NOT NULL,           -- 'BUY' | 'SELL'
    shares_micro    INTEGER NOT NULL,           -- share count * 1e6
    cost_micro      INTEGER NOT NULL,           -- USDC committed * 1e6
    entry_centi     INTEGER NOT NULL,           -- fill price * 1e4 (0-10000)
    cex_implied_prob REAL   NOT NULL,           -- CEX-derived probability at signal time
    edge_pct        REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    kelly_fraction  REAL    NOT NULL,
    order_id        TEXT,                       -- NULL for paper trades
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | CANCELLED
    exit_centi      INTEGER,
    pnl_micro       INTEGER,
    closed_ts       TEXT
);
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    contract_key    TEXT    NOT NULL,
    poly_centi      INTEGER NOT NULL,           -- Polymarket mid * 1e4
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
    equity_micro INTEGER NOT NULL,
    open_positions INTEGER NOT NULL DEFAULT 0,
    daily_pnl_micro INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_KILL_SWITCH_LOG = """
CREATE TABLE IF NOT EXISTS kill_switch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    equity_micro INTEGER NOT NULL
);
"""

# v1 stored money as REAL.  Those tables are renamed aside rather than dropped:
# a paper-trading history is research data, and silently deleting someone's
# trade log to change a column type is not a migration.
_LEGACY_SUFFIX = "_v1_real"
_LEGACY_TABLES = ("trades", "signals", "portfolio_snapshots", "kill_switch_log")


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
            self._migrate(conn)
            conn.executescript(
                _CREATE_TRADES
                + _CREATE_SIGNALS
                + _CREATE_SNAPSHOTS
                + _CREATE_KILL_SWITCH_LOG
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.debug("Database schema v%d initialised at %s", SCHEMA_VERSION, self._path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """
        Move a v1 (REAL-money) database aside so the v2 schema can be created.

        Float trade history cannot be converted into exact integers without
        inventing precision that was never there, so the old tables are
        preserved under a suffix for inspection and the bot starts a clean
        exact ledger.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return

        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not existing & set(_LEGACY_TABLES):
            return  # fresh database, nothing to move

        for table in _LEGACY_TABLES:
            if table not in existing:
                continue
            archived = f"{table}{_LEGACY_SUFFIX}"
            if archived in existing:
                continue  # already migrated once
            conn.execute(f"ALTER TABLE {table} RENAME TO {archived}")
            logger.warning(
                "Schema migration v%d: preserved float-precision table %r as %r. "
                "Historical rows are not carried over — the new ledger is exact "
                "and starts empty.",
                SCHEMA_VERSION, table, archived,
            )

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
        shares: Decimal,
        cost_usdc: Decimal,
        entry_price: Decimal,
        cex_implied_prob: float,
        edge_pct: float,
        confidence: float,
        kelly_fraction: float,
        order_id: str | None = None,
    ) -> int:
        sql = """
        INSERT INTO trades
            (ts, mode, contract_key, token_id, side, shares_micro, cost_micro,
             entry_centi, cex_implied_prob, edge_pct, confidence,
             kelly_fraction, order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        with self._conn() as conn:
            cur = conn.execute(
                sql,
                (
                    self._now(), mode, contract_key, token_id, side,
                    shares_to_micro(shares), usdc_to_micro(cost_usdc),
                    price_to_centi(entry_price), cex_implied_prob,
                    edge_pct, confidence, kelly_fraction, order_id,
                ),
            )
            trade_id = cur.lastrowid
        logger.info(
            "Trade #%d inserted (%s %s cost=%s USDC @ %s)",
            trade_id, side, contract_key, fmt_usdc(cost_usdc, 4), fmt_price(entry_price),
        )
        return trade_id  # type: ignore[return-value]

    def close_trade(
        self,
        trade_id: int,
        exit_price: Decimal,
        pnl_usdc: Decimal,
    ) -> None:
        sql = """
        UPDATE trades
        SET status='CLOSED', exit_centi=?, pnl_micro=?, closed_ts=?
        WHERE id=?
        """
        with self._conn() as conn:
            conn.execute(
                sql,
                (price_to_centi(exit_price), usdc_to_micro(pnl_usdc), self._now(), trade_id),
            )
        logger.info("Trade #%d closed – PnL %s USDC", trade_id, fmt_usdc(pnl_usdc, 4))

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

    @staticmethod
    def decode_trade(row: sqlite3.Row) -> dict[str, object]:
        """Decode a stored trade row's minor units back into Decimals."""
        return {
            "id":           row["id"],
            "ts":           row["ts"],
            "mode":         row["mode"],
            "contract_key": row["contract_key"],
            "token_id":     row["token_id"],
            "side":         row["side"],
            "shares":       micro_to_shares(row["shares_micro"]),
            "cost_usdc":    micro_to_usdc(row["cost_micro"]),
            "entry_price":  centi_to_price(row["entry_centi"]),
            "exit_price":   centi_to_price(row["exit_centi"]) if row["exit_centi"] is not None else None,
            "pnl_usdc":     micro_to_usdc(row["pnl_micro"]) if row["pnl_micro"] is not None else None,
            "status":       row["status"],
            "order_id":     row["order_id"],
            "closed_ts":    row["closed_ts"],
            "edge_pct":     row["edge_pct"],
            "confidence":   row["confidence"],
        }

    def get_daily_pnl(self) -> Decimal:
        """Sum of realised PnL for closed trades today (UTC).  Exact."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_micro), 0) FROM trades "
                "WHERE status='CLOSED' AND closed_ts >= ?",
                (today,),
            ).fetchone()
        return micro_to_usdc(row[0])

    def get_win_rate(self) -> tuple[int, int, float]:
        """Return (wins, total_closed, win_rate_pct)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN pnl_micro > 0 THEN 1 ELSE 0 END) as wins "
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
        poly_price: Decimal,
        cex_implied_prob: float,
        lag_pct: float,
        edge_pct: float,
        confidence: float,
        acted: bool = False,
        skip_reason: str | None = None,
    ) -> int:
        sql = """
        INSERT INTO signals
            (ts, contract_key, poly_centi, cex_implied_prob, lag_pct,
             edge_pct, confidence, acted, skip_reason)
        VALUES (?,?,?,?,?,?,?,?,?)
        """
        with self._conn() as conn:
            cur = conn.execute(
                sql,
                (
                    self._now(), contract_key, price_to_centi(poly_price),
                    cex_implied_prob, lag_pct, edge_pct, confidence,
                    int(acted), skip_reason,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def insert_snapshot(
        self,
        equity_usdc: Decimal,
        open_positions: int,
        daily_pnl: Decimal,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(ts, equity_micro, open_positions, daily_pnl_micro) "
                "VALUES (?,?,?,?)",
                (
                    self._now(), usdc_to_micro(equity_usdc),
                    open_positions, usdc_to_micro(daily_pnl),
                ),
            )

    def get_peak_equity_today(self) -> Decimal:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(equity_micro), 0) FROM portfolio_snapshots WHERE ts >= ?",
                (today,),
            ).fetchone()
        return micro_to_usdc(row[0])

    # ------------------------------------------------------------------
    # Kill-switch log
    # ------------------------------------------------------------------

    def log_kill_switch(self, reason: str, equity_usdc: Decimal) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kill_switch_log (ts, reason, equity_micro) VALUES (?,?,?)",
                (self._now(), reason, usdc_to_micro(equity_usdc)),
            )
        logger.critical("KILL SWITCH triggered: %s  equity=%s", reason, fmt_usdc(equity_usdc))
