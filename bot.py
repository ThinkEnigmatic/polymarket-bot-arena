#!/usr/bin/env python3
"""Phantom Swing Bot v0.1 – SOL/USDC paper trading

- Uses public price/ohlcv API (placeholder URL for now)
- 1-minute polling loop
- 1-hour signal timeframe
- Implements the rules from strategy.md (simplified where needed)

NOTE: This is a skeleton. It does NOT place real trades.
"""

import time
import json
import math
import pathlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
TRADES_FILE = BASE_DIR / "trades.jsonl"
EQUITY_FILE = BASE_DIR / "equity.jsonl"

INITIAL_EQUITY = 1000.0
FEE_RATE = 0.001  # 0.1% per side

# Symbols to scan (Binance spot as proxy). Fast movers can be added here.
SYMBOLS = [
    {"symbol": "SOLUSDT", "pair": "SOL/USDC"},
    {"symbol": "WIFUSDT", "pair": "WIF/USDC"},
    {"symbol": "BONKUSDT", "pair": "BONK/USDC"},
]

OHLC_API = "https://api.binance.com/api/v3/klines"
INTERVAL = "1h"
LIMIT = 200


@dataclass
class Position:
    side: str  # "long"
    entry_price: float
    size_base: float  # SOL
    notional: float
    leverage: float
    stop_price: float
    open_time: str


@dataclass
class AccountState:
    equity: float
    position: Optional[Position]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> AccountState:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        pos = data.get("position")
        position = Position(**pos) if pos else None
        return AccountState(equity=data["equity"], position=position)
    return AccountState(equity=INITIAL_EQUITY, position=None)


def save_state(state: AccountState) -> None:
    STATE_FILE.write_text(json.dumps({
        "equity": state.equity,
        "position": asdict(state.position) if state.position else None,
    }))


def log_trade(record: dict) -> None:
    with TRADES_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def log_equity(equity: float) -> None:
    rec = {"timestamp": now_iso(), "equity": equity}
    with EQUITY_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_ohlc(symbol: str) -> List[dict]:
    """Fetch 1h OHLC for a symbol via Binance (used as proxy)."""
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}
    resp = requests.get(OHLC_API, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()
    candles = []
    for k in raw:
        open_time = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        candles.append({
            "time": open_time,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        })
    return candles


def ema(values: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    result = []
    ema_prev = sum(values[:period]) / period
    result.extend([ema_prev] * period)
    for v in values[period:]:
        ema_prev = v * k + ema_prev * (1 - k)
        result.append(ema_prev)
    return result


def atr(candles: List[dict], period: int) -> List[float]:
    trs = []
    for i in range(len(candles)):
        c = candles[i]
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_close = candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )
        trs.append(tr)
    # simple ATR for v0.1
    atrs = []
    for i in range(len(trs)):
        if i + 1 < period:
            atrs.append(trs[i])
        else:
            atrs.append(sum(trs[i + 1 - period: i + 1]) / period)
    return atrs


def decide_entry(candles: List[dict], state: AccountState, pair_label: str) -> Optional[Position]:
    """Decide whether to open a long or short position based on v0.1.1 rules."""
    closes = [c["close"] for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = atr(candles, 14)

    c = candles[-1]
    close = c["close"]
    e20 = ema20[-1]
    e50 = ema50[-1]
    a14 = atr14[-1]

    swing_high = max(closes[-20:])
    swing_low = min(closes[-20:])

    vol_ratio = a14 / close if close > 0 else 0
    if not (0.005 <= vol_ratio <= 0.10):
        return None

    # Base risk
    R = 0.01 * state.equity
    stop_dist = 1.5 * a14
    if stop_dist <= 0:
        return None

    size_1x = R / stop_dist

    # Trend strength score (magnitude only)
    ts = abs(e20 - e50) / close if close > 0 else 0
    if ts < 0.005:
        lev = 1
    elif ts < 0.01:
        lev = 3
    elif ts < 0.02:
        lev = 5
    else:
        lev = 10

    size_base = size_1x * lev
    if size_base <= 0:
        return None

    # Long setup
    if e20 > e50 and close > e20 and close > swing_high:
        entry_price = close
        stop_price = close - stop_dist
        side = "long"

    # Short setup (mirror)
    elif e20 < e50 and close < e20 and close < swing_low:
        entry_price = close
        stop_price = close + stop_dist
        side = "short"

    else:
        return None

    notional = size_base * entry_price

    return Position(
        side=side,
        entry_price=entry_price,
        size_base=size_base,
        notional=notional,
        leverage=float(lev),
        stop_price=stop_price,
        open_time=now_iso(),
    )


def check_exit(candles: List[dict], pos: Position, equity: float, pair_label: str) -> Optional[dict]:
    """Return trade record if position should be closed, else None."""
    closes = [c["close"] for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = atr(candles, 14)

    c = candles[-1]
    close = c["close"]
    e20 = ema20[-1]
    e50 = ema50[-1]
    a14 = atr14[-1]

    entry = pos.entry_price
    stop = pos.stop_price

    # For long: stop when price <= stop; for short: stop when price >= stop
    if pos.side == "long":
        hit_stop = close <= stop
    else:  # short
        hit_stop = close >= stop

    if hit_stop:
        reason = "stop"
    else:
        # trend flip exit
        if pos.side == "long" and e20 < e50 and close < e50:
            reason = "trend_flip"
        elif pos.side == "short" and e20 > e50 and close > e50:
            reason = "trend_flip"
        else:
            # simple take profit at 3R
            if pos.side == "long":
                R = (entry - stop)
                r_mult = (close - entry) / R if R > 0 else 0
            else:
                R = (stop - entry)
                r_mult = (entry - close) / R if R > 0 else 0

            if r_mult >= 3:
                reason = "tp_3R"
            else:
                return None

    # PnL calculation
    if pos.side == "long":
        gross_pnl = (close - entry) * pos.size_base
    else:  # short
        gross_pnl = (entry - close) * pos.size_base

    fees = pos.notional * FEE_RATE * 2
    pnl = gross_pnl - fees

    new_equity = equity + pnl

    record = {
        "timestamp_open": pos.open_time,
        "timestamp_close": now_iso(),
        "pair": pair_label,
        "side": pos.side,
        "entry_price": entry,
        "exit_price": close,
        "size_base": pos.size_base,
        "notional_usdc": pos.notional,
        "leverage": pos.leverage,
        "initial_stop_price": stop,
        "final_stop_price": stop,
        "pnl_usdc": pnl,
        "pnl_pct": pnl / equity if equity > 0 else 0,
        "equity_after": new_equity,
        "reason_exit": reason,
    }

    return {"record": record, "new_equity": new_equity}


def main_loop():
    state = load_state()
    print(f"Starting bot – equity {state.equity:.2f} USDC")

    while True:
        # log equity every tick
        log_equity(state.equity)

        best_signal = None

        # scan multiple symbols, choose the strongest signal by trend strength
        for sym in SYMBOLS:
            try:
                candles = fetch_ohlc(sym["symbol"])
            except Exception as e:
                print(f"Error fetching data for {sym['symbol']}:", e)
                continue

            pos_candidate = decide_entry(candles, state, sym["pair"])
            if not pos_candidate:
                continue

            closes = [c["close"] for c in candles]
            ema20 = ema(closes, 20)
            ema50 = ema(closes, 50)
            ts = abs(ema20[-1] - ema50[-1]) / closes[-1]

            score = ts
            if best_signal is None or score > best_signal["score"]:
                best_signal = {"pos": pos_candidate, "pair": sym["pair"], "score": score}

        if state.position is None:
            if best_signal:
                pos = best_signal["pos"]
                print(f"ENTER {pos.side} {best_signal['pair']} @ {pos.entry_price:.3f}, size {pos.size_base:.4f}, lev {pos.leverage}")
                state.position = pos
                save_state(state)
        else:
            # manage existing position using SOL candles as proxy
            # TODO: per-pair state; for v0.2 we reuse the first symbol's data
            try:
                candles = fetch_ohlc(SYMBOLS[0]["symbol"])
            except Exception as e:
                print("Error fetching data for exit check:", e)
                time.sleep(60)
                continue

            res = check_exit(candles, state.position, state.equity, state.position.side + " " + "multi")
            if res:
                rec = res["record"]
                state.equity = res["new_equity"]
                print(f"EXIT {rec['pair']} {rec['reason_exit']} PnL {rec['pnl_usdc']:.2f}, equity {state.equity:.2f}")
                log_trade(rec)
                state.position = None
                save_state(state)

        time.sleep(60)


if __name__ == "__main__":
    main_loop()
