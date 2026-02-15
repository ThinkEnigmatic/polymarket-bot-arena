# Polymarket Bot Arena — Developer Guide

## What This Is

An automated trading bot arena that runs 4 competing bots on Polymarket's BTC 5-minute up/down markets via the Simmer paper trading platform. Bots evolve every 4 hours — the bottom 2 are replaced by mutated copies of the top 2. Each bot has its own Simmer account for independent trading and real performance comparison.

## Current State (v4 — Feb 15, 2026)

**GitHub:** https://github.com/ThinkEnigmatic/polymarket-bot-arena.git (branch: main)

### Performance (276 real trades)
- **Total P&L: -$52.10** (mostly from old contrarian/overconfident bets)
- momentum-v1: 55.8% WR, -$1.54 (nearly breakeven, BEST bot)
- hybrid-v1: 45.5% WR, -$7.47
- meanrev-v1: 40.4% WR, -$17.72
- sentiment-v1: 40.0% WR, -$25.36

### Key Data Insights (use these for future iterations)
- **Market price is the strongest signal** — when YES is priced >65c, YES wins ~100% of the time
- **Contrarian/mean-reversion strategies lose money** in 5-min markets
- **Confidence 0.30-0.50 is the sweet spot** — 67.9% WR, +$48 total
- **Confidence >0.50 LOSES money** — 48.6% WR but large bet sizes = big losses
- **NO bets have 44.9% WR vs YES at 49.2%** — slight YES bias is profitable
- **Buying cheap YES (<40c) against market consensus = 0-10% WR** (catastrophic)

### What's Running
- **Arena process:** launchd service `com.polymarket.botarena` — auto-restarts on crash
- **Dashboard:** launchd service `com.polymarket.dashboard` — FastAPI on port 8050
- **Remote access:** localtunnel (not persistent, needs manual restart: `npx localtunnel --port 8050`)
- **Price feed:** Binance WebSocket for BTC/USDT 1-min candles

### Simmer API Keys (4 accounts, slot-based)
Stored at `~/.config/simmer/bot_keys.json` — keys mapped to slot_0 through slot_3. When evolution kills a bot, the replacement inherits the dead bot's slot (and API key). Default key at `~/.config/simmer/simmer_api_key.json`.

## Architecture

### Signal Hierarchy (make_decision in base_bot.py)
```
combined = (
    market_price_edge * 0.50    # Strongest: follow the market price
    + btc_momentum * 0.20       # BTC price movement direction
    + strategy_signal * 0.15    # Per-bot strategy differentiation
    + learning_bias * variable  # Grows from 10% to 60% weight with data
)
```

### Safeguards
- **Market consensus guard:** Never bet against prices >65c or <35c
- **Bet sizing cap:** Confidence capped at 0.45 for sizing (prevents overconfident large bets)
- **Stale trade expiry:** Trades pending >1h auto-expire (5-min markets resolve in ~10min)
- **Daily loss limits:** Uncapped for paper trading (was $10/bot, $25 total)
- **Dedup:** Loads recent (bot, market) pairs from DB to prevent duplicates across restarts

### Per-Strategy Differentiation
| Strategy | Aggression | Prior | Min Confidence |
|----------|-----------|-------|----------------|
| momentum | 1.2 (follows price strongly) | 0.52 (slight YES) | 0.01 (trades almost everything) |
| mean_reversion | 0.95 (nearly follows, was 0.6) | 0.48 (slight NO) | 0.06 |
| sentiment | 1.0 (neutral) | 0.50 | 0.03 |
| hybrid | 1.0 (neutral, was 0.9) | 0.50 | 0.05 |

### Learning System
- Features extracted at TRADE TIME (not resolution time — this was a critical bug fix)
- Stored in `trade_features` column in trades table
- Learning records win/loss by feature bucket (price level + momentum)
- Weight ramps from 10% to 60% as bot accumulates resolved trades
- Learning data in `bot_learning` table

## Key Files

```
arena.py              # Main loop: discover markets, run bots, resolve trades, evolve
bots/base_bot.py      # BaseBot with make_decision() signal hierarchy + execute()
bots/bot_momentum.py  # MomentumBot (follows trends)
bots/bot_mean_rev.py  # MeanRevBot (was contrarian, now nearly neutral)
bots/bot_sentiment.py # SentimentBot
bots/bot_hybrid.py    # HybridBot
config.py             # All config: paths, limits, evolution interval, API URLs
db.py                 # SQLite: trades, bot_configs, evolution_events, bot_learning
learning.py           # Feature extraction, bias calculation, outcome recording
signals/price_feed.py # Binance WS for BTC candles (staleness detection)
signals/sentiment.py  # Sentiment signals
signals/orderflow.py  # Order flow signals
dashboard/server.py   # FastAPI dashboard backend
dashboard/index.html  # Dashboard frontend
copytrading/          # Wallet tracking + copy trading (not actively used)
```

### launchd Services
```
~/Library/LaunchAgents/com.polymarket.botarena.plist  → arena.py
~/Library/LaunchAgents/com.polymarket.dashboard.plist → dashboard/server.py
```

### Database
SQLite at `trading_bot/data/arena.db` — tables: trades, bot_configs, evolution_events, daily_stats, bot_learning, copytrading_wallets, copytrading_trades

## Bug History (avoid re-introducing)

1. **Circular learning (v3 bug, CRITICAL):** `resolve_trades()` used `market.get("current_price")` at resolution time. Resolved markets have price ~1.0 or ~0.0, so learning was: "high price = YES wins" (tautology). **Fix:** Store features at trade time in `trade_features` column.

2. **P&L always $0 (v3 bug):** Old `resolve_trades()` SELECT didn't include `shares_bought`, so the try/except defaulted to shares=0, making pnl=0 for all trades. **Fix:** Include shares_bought in SELECT, formula: `pnl = (shares - amount) if won else -amount`.

3. **P&L formula wrong (v3 bug):** Used `pnl = amount` for wins instead of `shares_bought - amount`. In prediction markets, profit = shares * $1 - cost, not 2x the bet.

4. **Stale trades clogging queue:** 5-min market trades that fell off Simmer's resolved API (limit=200) stayed pending forever (425 trades). **Fix:** Auto-expire trades pending >1h.

5. **Duplicate trades on restart:** In-memory `traded` set reset every restart. **Fix:** Load recent (bot, market) pairs from DB on startup.

6. **sqlite3.Row has no .get():** Use `try: val = row["col"] except: val = default` instead of `row.get("col", default)`.

## Next Steps for Iteration

### Priority 1: Let v4 accumulate data
The v4 fixes (consensus guard, bet sizing cap, aggression fix) need 50+ resolved trades with stored features to evaluate. Check after ~2-4 hours of running.

### Priority 2: Verify learning is working correctly
Once trades with stored features start resolving, verify:
```python
# In python3 from trading_bot dir:
import db
with db.get_conn() as conn:
    rows = conn.execute("SELECT * FROM bot_learning ORDER BY updated_at DESC LIMIT 20").fetchall()
    for r in rows: print(dict(r))
```

### Priority 3: Analyze v4 performance
After 50+ resolved trades with features, run the analysis:
```python
import db
with db.get_conn() as conn:
    # Compare pre-v4 vs post-v4 by checking trades after the restart
    rows = conn.execute('''
        SELECT bot_name, side, COUNT(*) as trades,
            SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as pnl
        FROM trades WHERE outcome IN ('win','loss') AND trade_features IS NOT NULL
        GROUP BY bot_name
    ''').fetchall()
    for r in rows: print(dict(r))
```

### Priority 4: Future improvements to explore
- **Time-of-day analysis:** Do certain hours have better WR?
- **BTC volatility filter:** Skip trading during low-volatility periods (no edge)
- **Adaptive confidence thresholds:** Adjust min_confidence based on recent WR
- **Ensemble voting (optional):** When 3+ bots agree, increase bet size
- **Live trading readiness:** Once consistently profitable in paper, consider switching to live

### User Incentive
"$10 in tokens for every $100 earned" — both user and bot benefit from profitability.
