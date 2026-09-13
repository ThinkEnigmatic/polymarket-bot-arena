# Polymarket Bot Arena

An adaptive trading bot system that runs four competing strategies on Polymarket's BTC five-minute up/down markets. Bots learn from resolved trades and evolve against realized P&L.

> Trading can lose money. The safeguards below bound configured exposure and prevent known accounting/execution errors; they do not guarantee profit or eliminate market, liquidity, API, wallet, or software risk. Keep bots in paper mode until results are statistically meaningful.

## How It Works

**4 competing bots** trade every active BTC 5-min market on Polymarket (via Simmer for paper trading):

| Bot | Strategy | Description |
|-----|----------|-------------|
| `momentum-v1` | Trend Following | Trades in the direction of short-term BTC price momentum |
| `meanrev-v1` | Mean Reversion | Bets against overextended moves using z-score and RSI |
| `sentiment-v1` | Sentiment | Uses social/news sentiment signals |
| `hybrid-v1` | Ensemble | Weighted combination of all three strategies |

**Adaptive learning**: Each bot tracks win rates by market conditions (price bucket, BTC momentum, time of day). After every resolved trade, outcomes feed back into a Bayesian learning model that adjusts future decisions. More data = smarter bots.

**Evolution**: Every two hours, bots with at least 20 resolved trades are ranked by realized P&L. Tested money-losers are replaced with mutated children of profitable strategy families. Untested bots remain immune until they have enough observations.

## Architecture

```
trading_bot/
  arena.py           # Main loop: market discovery, trading, resolution, evolution
  learning.py         # Bayesian learning engine (feature extraction + win rate tracking)
  db.py               # SQLite: trades, bot configs, evolution history, learning data
  config.py           # Risk limits, API config, paper/live toggle
  setup.py            # Account setup & verification
  polymarket_client.py # Direct Polymarket CLOB client (for live trading)
  bots/
    base_bot.py       # Abstract base with make_decision() (strategy + learning)
    bot_momentum.py   # Momentum strategy
    bot_mean_rev.py   # Mean reversion strategy
    bot_sentiment.py  # Sentiment strategy
    bot_hybrid.py     # Ensemble strategy
  signals/
    price_feed.py     # Real-time BTC prices via Binance WebSocket
    sentiment.py      # Social sentiment scoring
    orderflow.py      # Polymarket order flow signals
  copytrading/
    tracker.py        # Track top-performing wallets
    copier.py         # Mirror trades from tracked wallets
  dashboard/
    server.py         # FastAPI dashboard backend
    index.html        # Real-time web dashboard with market timers
```

## Setup

### Prerequisites

- Python 3.10+
- A [Simmer](https://simmer.markets) account (free, for paper trading)

### Install

```bash
pip install websocket-client requests fastapi uvicorn
```

### Configure

1. Get your Simmer API key from https://simmer.markets
2. Save it:
```bash
mkdir -p ~/.config/simmer
echo '{"api_key": "your-key-here"}' > ~/.config/simmer/credentials.json
```

3. Run setup to verify:
```bash
python setup.py
```

### Run

```bash
# Start the arena (paper trading)
python arena.py

# Start the dashboard (separate terminal)
export ARENA_DASHBOARD_PASSWORD='use-a-long-unique-password'
python dashboard/server.py
# Open http://localhost:8501
```

The dashboard user defaults to `admin`; override it with `ARENA_DASHBOARD_USER`.
Keep credentials and machine-specific paths out of commits. The launchd plist
files contain an `/opt/polymarket-bot-arena` deployment placeholder; set the
correct path only in your private local installation.

## Dashboard

Real-time web dashboard showing:
- P&L stats (today / week / all time)
- Per-bot performance with win rates
- Active BTC 5-min market countdown timers
- Recent trades with outcomes
- Evolution history
- Daily earnings chart

## Paper vs Live Trading

The system starts in **paper mode** using Simmer's $SIM currency. To switch to live trading with real USDC on Polymarket:

1. Save your Polymarket wallet private key to `~/.config/polymarket/credentials.json`
2. Toggle via the dashboard button or `--mode live` flag
3. Live mode has stricter risk limits ($10/trade vs $50)

## Risk Limits

| Setting | Paper | Live |
|---------|-------|------|
| Max per trade | $50 SIM | $10 USDC |
| Max daily risk per bot | Uncapped | $50 USDC |
| Max daily risk total | Uncapped | $100 USDC |

Daily risk is conservative: it includes gross realized losses plus the full cost of unresolved positions. A new order is clamped to the remaining bot and arena budgets. Wins do not offset the daily loss counter.

Live taker orders use fill-or-kill semantics, enforce a maximum price drift of two cents from the observed market price, and are recorded using the CLOB's actual filled collateral and shares. Experimental maker bots remain paper-only until asynchronous fill and cancellation reconciliation is implemented.

Only strict BTC five-minute markets are eligible, and only while their measurement window is active with at least 60 seconds remaining. Markets with missing timestamps, longer durations, or future windows fail closed.

### Verification

```bash
python -m unittest discover -s tests -v
```

## License

MIT
