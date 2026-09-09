"""
Scanner configuration.

Everything that could cost money is off by default. DRY_RUN in particular is
opt-out, not opt-in, and requires an explicit environment variable rather than
a truthy value, so a stray "0" or empty string cannot enable live trading.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Final

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(_env(name, default) or default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


# ----------------------------------------------------------------------
# Safety
# ----------------------------------------------------------------------

# Live order placement requires this exact string. Anything else - unset,
# "false", "0", "no", a typo - keeps the scanner in DRY_RUN.
_LIVE_SENTINEL: Final[str] = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"

DRY_RUN: Final[bool] = _env("KALSHI_ARB_LIVE") != _LIVE_SENTINEL


def live_trading_enabled() -> bool:
    return not DRY_RUN


# ----------------------------------------------------------------------
# Credentials (read-only scanning needs none of these)
# ----------------------------------------------------------------------

KALSHI_KEY_ID: Final[str] = _env("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_PATH: Final[str] = _env("KALSHI_PRIVATE_KEY_PATH")
KALSHI_USE_DEMO: Final[bool] = _env("KALSHI_USE_DEMO", "false").lower() == "true"

# ----------------------------------------------------------------------
# Rate limits — verify against the live published schedule
# ----------------------------------------------------------------------

KALSHI_BUCKET_CAPACITY: Final[float] = float(_env("KALSHI_BUCKET_CAPACITY", "100"))
KALSHI_REFILL_PER_SEC: Final[float] = float(_env("KALSHI_REFILL_PER_SEC", "10"))
POLY_BUCKET_CAPACITY: Final[float] = float(_env("POLY_BUCKET_CAPACITY", "60"))
POLY_REFILL_PER_SEC: Final[float] = float(_env("POLY_REFILL_PER_SEC", "6"))

# ----------------------------------------------------------------------
# Scanning thresholds
# ----------------------------------------------------------------------

# Minimum net profit per contract, in dollars, before a spread is reported.
# Below roughly half a cent the edge is inside the noise of both books and
# will not survive the latency between filling one leg and the other.
MIN_NET_EDGE_PER_CONTRACT: Final[Decimal] = _decimal("MIN_NET_EDGE_PER_CONTRACT", "0.005")

# Minimum contracts that must clear on both legs for a spread to be worth
# acting on. A two-contract arb does not pay for the attention.
MIN_CONTRACTS: Final[int] = _int("MIN_CONTRACTS", 10)

BOOK_DEPTH: Final[int] = _int("BOOK_DEPTH", 10)

# ----------------------------------------------------------------------
# Daemon (continuous local operation)
# ----------------------------------------------------------------------

# Seconds between scan cycles. Cross-venue spreads on event markets persist
# for minutes to hours, not milliseconds - this is not a latency race, and a
# tighter interval mostly spends rate-limit budget to re-read unchanged books.
SCAN_INTERVAL_SEC: Final[float] = float(_env("SCAN_INTERVAL_SEC", "60"))
MAX_BACKOFF_SEC: Final[float] = float(_env("MAX_BACKOFF_SEC", "900"))

# Do not re-alert the same (pair, direction) inside this window unless its
# net edge per contract improved by at least ALERT_IMPROVEMENT.
ALERT_COOLDOWN_SEC: Final[float] = float(_env("ALERT_COOLDOWN_SEC", "3600"))
ALERT_IMPROVEMENT: Final[Decimal] = _decimal("ALERT_IMPROVEMENT", "0.01")

ALERT_ON_RESEARCH: Final[bool] = _env("ALERT_ON_RESEARCH", "false").lower() == "true"

TELEGRAM_BOT_TOKEN: Final[str] = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Final[str] = _env("TELEGRAM_CHAT_ID")
MACOS_NOTIFICATIONS: Final[bool] = _env("MACOS_NOTIFICATIONS", "true").lower() == "true"

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------

DATA_DIR: Final[Path] = Path(_env("KALSHI_ARB_DATA_DIR", "") or Path(__file__).parent / "data")
PAIRS_PATH: Final[Path] = DATA_DIR / "pairs.json"
ALERT_STATE_PATH: Final[Path] = DATA_DIR / "alert_state.json"
HEARTBEAT_PATH: Final[Path] = DATA_DIR / "heartbeat.json"
LOG_PATH: Final[Path] = Path(_env("KALSHI_ARB_LOG_PATH", "") or DATA_DIR / "scanner.log")

LOG_LEVEL: Final[str] = _env("LOG_LEVEL", "INFO")


def describe() -> str:
    mode = "DRY_RUN (no orders will be placed)" if DRY_RUN else "*** LIVE ORDER PLACEMENT ***"
    return (
        f"Mode                : {mode}\n"
        f"Kalshi base         : {'demo' if KALSHI_USE_DEMO else 'production'}\n"
        f"Kalshi credentials  : {'configured' if KALSHI_KEY_ID else 'not set (read-only)'}\n"
        f"Min net edge        : ${MIN_NET_EDGE_PER_CONTRACT}/contract\n"
        f"Min contracts       : {MIN_CONTRACTS}\n"
        f"Scan interval       : {SCAN_INTERVAL_SEC:.0f}s\n"
        f"Alert cooldown      : {ALERT_COOLDOWN_SEC:.0f}s "
        f"(re-alert on +{ALERT_IMPROVEMENT}/contract)\n"
        f"Alert channels      : {', '.join(_active_channels()) or 'console only'}\n"
        f"Pairs file          : {PAIRS_PATH}\n"
        f"Heartbeat           : {HEARTBEAT_PATH}"
    )


def _active_channels() -> list[str]:
    out = ["console"]
    if MACOS_NOTIFICATIONS:
        out.append("macos")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        out.append("telegram")
    return out
