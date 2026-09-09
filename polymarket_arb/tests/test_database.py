"""Persistence exactness and the v1 -> v2 schema migration."""
from decimal import Decimal
import sqlite3

import pytest

from database import Database, SCHEMA_VERSION
from money import ZERO


@pytest.fixture
def db(tmp_path):
    return Database(path=tmp_path / "test.db")


def add_trade(db, *, shares="270.270270", cost="100.000000", entry="0.37"):
    return db.insert_trade(
        mode="paper", contract_key="BTC_5M_UP", token_id="tok", side="BUY",
        shares=Decimal(shares), cost_usdc=Decimal(cost), entry_price=Decimal(entry),
        cex_implied_prob=0.55, edge_pct=3.0, confidence=90.0, kelly_fraction=0.5,
    )


def test_schema_version_stamped(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_money_columns_are_integer_not_real(db, tmp_path):
    """REAL affinity is what re-introduced float error on every read."""
    conn = sqlite3.connect(tmp_path / "test.db")
    types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(trades)")}
    conn.close()
    for col in ("shares_micro", "cost_micro", "entry_centi", "exit_centi", "pnl_micro"):
        assert types[col] == "INTEGER", f"{col} is {types[col]}"


def test_trade_roundtrip_is_exact(db):
    tid = add_trade(db)
    row = Database.decode_trade(db.get_recent_trades(1)[0])
    assert row["shares"] == Decimal("270.270270")
    assert row["cost_usdc"] == Decimal("100.000000")
    assert row["entry_price"] == Decimal("0.3700")
    assert row["id"] == tid


def test_close_roundtrip_is_exact(db):
    tid = add_trade(db)
    db.close_trade(tid, Decimal("0.4321"), Decimal("-12.345678"))
    row = Database.decode_trade(db.get_recent_trades(1)[0])
    assert row["exit_price"] == Decimal("0.4321")
    assert row["pnl_usdc"] == Decimal("-12.345678")
    assert row["status"] == "CLOSED"


def test_daily_pnl_sum_is_exact_over_many_trades(db):
    """
    SUM over REAL accumulates error; SUM over INTEGER cannot. This is the
    figure the drawdown kill-switch reads.
    """
    each = Decimal("0.333333")
    n = 3000
    for _ in range(n):
        tid = add_trade(db)
        db.close_trade(tid, Decimal("0.3800"), each)
    assert db.get_daily_pnl() == each * n


def test_open_trades_and_win_rate(db):
    for _ in range(3):
        tid = add_trade(db)
        db.close_trade(tid, Decimal("0.40"), Decimal("1.5"))
    losing = add_trade(db)
    db.close_trade(losing, Decimal("0.30"), Decimal("-1.5"))
    add_trade(db)  # left open

    assert len(db.get_open_trades()) == 1
    wins, total, rate = db.get_win_rate()
    assert (wins, total) == (3, 4)
    assert rate == pytest.approx(75.0)


def test_snapshot_and_peak_equity_exact(db):
    db.insert_snapshot(equity_usdc=Decimal("1000.123456"), open_positions=1, daily_pnl=ZERO)
    db.insert_snapshot(equity_usdc=Decimal("1200.654321"), open_positions=0, daily_pnl=ZERO)
    assert db.get_peak_equity_today() == Decimal("1200.654321")


def test_empty_db_returns_zero_not_none(db):
    assert db.get_daily_pnl() == ZERO
    assert db.get_peak_equity_today() == ZERO


# ----------------------------------------------------------------------
# Migration
# ----------------------------------------------------------------------

_V1_TRADES = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, mode TEXT NOT NULL,
    contract_key TEXT NOT NULL, token_id TEXT NOT NULL, side TEXT NOT NULL,
    size_usdc REAL NOT NULL, entry_price REAL NOT NULL,
    cex_implied_prob REAL NOT NULL, edge_pct REAL NOT NULL,
    confidence REAL NOT NULL, kelly_fraction REAL NOT NULL, order_id TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN', exit_price REAL, pnl_usdc REAL,
    closed_ts TEXT
);
"""


def test_v1_database_is_preserved_not_destroyed(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_V1_TRADES)
    conn.execute(
        "INSERT INTO trades (ts, mode, contract_key, token_id, side, size_usdc, "
        "entry_price, cex_implied_prob, edge_pct, confidence, kelly_fraction) "
        "VALUES ('2026-01-01','paper','BTC_5M_UP','t','BUY',100.0,0.37,0.5,3.0,90.0,0.5)"
    )
    conn.commit()
    conn.close()

    Database(path=path)  # triggers migration

    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "trades_v1_real" in tables, "legacy trade history must not be dropped"
    assert conn.execute("SELECT COUNT(*) FROM trades_v1_real").fetchone()[0] == 1
    # New ledger is present, exact, and empty.
    types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(trades)")}
    assert types["cost_micro"] == "INTEGER"
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_V1_TRADES)
    conn.commit()
    conn.close()

    Database(path=path)
    Database(path=path)  # second open must not re-archive or raise

    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "trades_v1_real_v1_real" not in tables
    conn.close()


def test_fresh_db_is_not_treated_as_legacy(tmp_path):
    Database(path=tmp_path / "fresh.db")
    conn = sqlite3.connect(tmp_path / "fresh.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(t.endswith("_v1_real") for t in tables)
    conn.close()
