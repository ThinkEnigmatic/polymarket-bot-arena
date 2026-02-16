"""
Polymarket Bot Arena Configuration
"""

import os
from pathlib import Path

# Trading Mode: "paper" (default, uses $SIM) or "live" (real USDC)
TRADING_MODE = "paper"  # MUST start in paper mode

# Simmer API Configuration
SIMMER_API_KEY_PATH = Path.home() / ".config/simmer/credentials.json"
SIMMER_BASE_URL = "https://api.simmer.markets"

# Multi-agent: each bot gets its own Simmer account for independent trading
# Keys are mapped bot_name -> api_key. Falls back to the default key.
SIMMER_BOT_KEYS_PATH = Path.home() / ".config/simmer/bot_keys.json"

# Polymarket Direct CLOB (for live trading)
POLYMARKET_KEY_PATH = Path.home() / ".config/polymarket/credentials.json"
POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_CHAIN_ID = 137  # Polygon

# Database
DB_PATH = Path(__file__).parent / "bot_arena.db"

# Target Market: BTC 5-min up/down
TARGET_MARKET_QUERY = "btc"  # Search term for market discovery
TARGET_MARKET_KEYWORDS = ["5 min", "5-min", "5min", "up or down", "up/down"]
BTC_5MIN_MARKET_ID = None  # Will be populated by setup.py

# Risk Limits - Paper Mode (default) — no caps, let bots compete freely
PAPER_MAX_POSITION = 50.0  # $SIM per trade
PAPER_MAX_DAILY_LOSS_PER_BOT = 999999.0  # Uncapped for paper
PAPER_MAX_DAILY_LOSS_TOTAL = 999999.0  # Uncapped for paper
PAPER_STARTING_BALANCE = 10000.0  # $SIM

# Risk Limits - Live Mode (stricter)
LIVE_MAX_POSITION = 10.0  # USDC per trade
LIVE_MAX_DAILY_LOSS_PER_BOT = 50.0  # USDC
LIVE_MAX_DAILY_LOSS_TOTAL = 100.0  # USDC

# General Risk Rules (both modes)
MAX_POSITION_PCT_OF_BALANCE = 0.10  # Never bet more than 10% of balance
MAX_TRADES_PER_HOUR_PER_BOT = 60  # Bots trade every 5-min market they find

# Evolution Settings
EVOLUTION_INTERVAL_HOURS = 2
MUTATION_RATE = 0.15  # 15% random adjustment to params
NUM_BOTS = 4
SURVIVORS_PER_CYCLE = 1  # Top 1 survives, bottom 3 replaced
MIN_TRADES_FOR_JUDGMENT = 20   # Bots with fewer resolved trades are immune
MIN_WIN_RATE = 0.70            # 70% WR threshold to survive evolution

# Signal Feed Settings
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
PRICE_UPDATE_INTERVAL_SEC = 1  # Real-time price updates

# Copy Trading Settings
COPYTRADING_ENABLED = True
COPYTRADING_MAX_WALLETS_TO_TRACK = 10
COPYTRADING_POSITION_SIZE_FRACTION = 0.5  # Copy 50% of whale's position size

# Dashboard Settings
DASHBOARD_PORT = 8501
DASHBOARD_HOST = "0.0.0.0"

# Logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def get_current_mode():
    """Get current trading mode"""
    return TRADING_MODE


def get_max_position():
    """Get max position size based on current mode"""
    return LIVE_MAX_POSITION if TRADING_MODE == "live" else PAPER_MAX_POSITION


def get_max_daily_loss_per_bot():
    """Get max daily loss per bot based on current mode"""
    return LIVE_MAX_DAILY_LOSS_PER_BOT if TRADING_MODE == "live" else PAPER_MAX_DAILY_LOSS_PER_BOT


def get_max_daily_loss_total():
    """Get max total daily loss based on current mode"""
    return LIVE_MAX_DAILY_LOSS_TOTAL if TRADING_MODE == "live" else PAPER_MAX_DAILY_LOSS_TOTAL


def get_venue():
    """Get trading venue based on current mode"""
    return "polymarket" if TRADING_MODE == "live" else "simmer"


def set_trading_mode(mode: str):
    """
    Set trading mode (paper or live)
    NOTE: This only updates the runtime config, not the config.py file
    For persistence, use the dashboard or manually edit config.py
    """
    global TRADING_MODE
    if mode not in ["paper", "live"]:
        raise ValueError("Mode must be 'paper' or 'live'")
    TRADING_MODE = mode
    return TRADING_MODE
