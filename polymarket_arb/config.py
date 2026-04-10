"""
Central configuration for the Polymarket latency arbitrage bot.

All tuneable parameters live here so that nothing is hard-coded
in business logic modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR: Final[Path] = Path(__file__).parent
DB_PATH: Final[Path] = BASE_DIR / "trades.db"
LOG_PATH: Final[Path] = BASE_DIR / "bot.log"

# ---------------------------------------------------------------------------
# Polymarket CLOB
# ---------------------------------------------------------------------------
CLOB_HOST: Final[str] = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
POLY_PRIVATE_KEY: Final[str] = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: Final[str] = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: Final[str] = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: Final[str] = os.getenv("POLY_API_PASSPHRASE", "")
# Chain ID: 137 = Polygon mainnet, 80002 = Amoy testnet
CHAIN_ID: Final[int] = int(os.getenv("CHAIN_ID", "137"))

# Token IDs for the contracts we track.
# Keys follow the pattern  {ASSET}_{WINDOW}_{DIRECTION}
# Fill in real token IDs from Polymarket before going live.
CONTRACT_TOKEN_IDS: Final[dict[str, str]] = {
    "BTC_5M_UP":   os.getenv("TOKEN_BTC_5M_UP",   ""),
    "BTC_5M_DOWN": os.getenv("TOKEN_BTC_5M_DOWN",  ""),
    "BTC_15M_UP":  os.getenv("TOKEN_BTC_15M_UP",   ""),
    "BTC_15M_DOWN":os.getenv("TOKEN_BTC_15M_DOWN",  ""),
    "ETH_5M_UP":   os.getenv("TOKEN_ETH_5M_UP",   ""),
    "ETH_5M_DOWN": os.getenv("TOKEN_ETH_5M_DOWN",  ""),
    "ETH_15M_UP":  os.getenv("TOKEN_ETH_15M_UP",   ""),
    "ETH_15M_DOWN":os.getenv("TOKEN_ETH_15M_DOWN",  ""),
}

# Poll interval for CLOB orderbook updates (seconds)
CLOB_POLL_INTERVAL: Final[float] = float(os.getenv("CLOB_POLL_INTERVAL", "2.0"))

# ---------------------------------------------------------------------------
# Binance WebSocket
# ---------------------------------------------------------------------------
BINANCE_WS_BASE: Final[str] = "wss://stream.binance.com:9443/stream"
BINANCE_SYMBOLS: Final[list[str]] = ["btcusdt", "ethusdt"]

# ---------------------------------------------------------------------------
# Arbitrage signal thresholds
# ---------------------------------------------------------------------------
# Minimum price lag between Polymarket implied prob and CEX-derived prob
LAG_THRESHOLD_PCT: Final[float] = float(os.getenv("LAG_THRESHOLD_PCT", "3.0"))
# Minimum edge after fees before we consider a trade
MIN_EDGE_PCT: Final[float] = float(os.getenv("MIN_EDGE_PCT", "5.0"))
# Minimum model confidence (0–100) required to execute
MIN_CONFIDENCE: Final[float] = float(os.getenv("MIN_CONFIDENCE", "85.0"))
# Maximum fraction of portfolio in any single position (0–1)
MAX_POSITION_FRACTION: Final[float] = float(os.getenv("MAX_POSITION_FRACTION", "0.08"))

# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------
# We use half-Kelly to dampen variance
KELLY_FRACTION: Final[float] = float(os.getenv("KELLY_FRACTION", "0.5"))

# ---------------------------------------------------------------------------
# Portfolio / risk
# ---------------------------------------------------------------------------
STARTING_PORTFOLIO_USDC: Final[Decimal] = Decimal(
    os.getenv("STARTING_PORTFOLIO_USDC", "1000")
)
# Daily drawdown kill-switch threshold (fraction, e.g. 0.20 = 20 %)
MAX_DAILY_DRAWDOWN: Final[float] = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.20"))

# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
# Paper-trading is the DEFAULT.  Switching to live requires all three flags.
PAPER_TRADING: Final[bool] = os.getenv("PAPER_TRADING", "true").lower() != "false"
LIVE_CONFIRM_1: Final[bool] = os.getenv("LIVE_CONFIRM_1", "false").lower() == "true"
LIVE_CONFIRM_2: Final[bool] = os.getenv("LIVE_CONFIRM_2", "false").lower() == "true"
LIVE_CONFIRM_3: Final[bool] = os.getenv("LIVE_CONFIRM_3", "false").lower() == "true"


def is_live_trading() -> bool:
    """Return True only when paper mode is disabled AND all three live-confirms are set."""
    return (
        not PAPER_TRADING
        and LIVE_CONFIRM_1
        and LIVE_CONFIRM_2
        and LIVE_CONFIRM_3
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: Final[str] = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: Final[str] = os.getenv("TELEGRAM_CHAT_ID", "")
# Drawdown level that triggers a Telegram alert (fraction)
TELEGRAM_DRAWDOWN_ALERT_PCT: Final[float] = float(
    os.getenv("TELEGRAM_DRAWDOWN_ALERT_PCT", "0.10")
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO")


@dataclass(frozen=True)
class WindowConfig:
    """Description of a single price-window contract pair."""
    asset: str          # "BTC" or "ETH"
    window_min: int     # 5 or 15
    up_token: str
    down_token: str

    @property
    def key(self) -> str:
        return f"{self.asset}_{self.window_min}M"


def get_window_configs() -> list[WindowConfig]:
    """Return WindowConfig objects for every configured contract pair."""
    configs = []
    for asset in ("BTC", "ETH"):
        for window in (5, 15):
            up_key   = f"{asset}_{window}M_UP"
            down_key = f"{asset}_{window}M_DOWN"
            up_tok   = CONTRACT_TOKEN_IDS.get(up_key, "")
            down_tok = CONTRACT_TOKEN_IDS.get(down_key, "")
            configs.append(WindowConfig(
                asset=asset,
                window_min=window,
                up_token=up_tok,
                down_token=down_tok,
            ))
    return configs
