# Polymarket Bot Arena

An adaptive trading bot system that runs 4 competing strategies on Polymarket's BTC 5-minute up/down markets. Bots learn from every resolved trade and continuously improve their win rates.

## How It Works

**4 competing bots** trade every active BTC 5-min market on Polymarket (via Simmer for paper trading):

| Bot | Strategy | Description |
|-----|----------|-------------|
| `momentum-v1` | Trend Following | Trades in the direction of short-term BTC price momentum |
| `meanrev-v1` | Mean Reversion | Bets against overextended moves using z-score and RSI |
| `sentiment-v1` | Sentiment | Uses social/news sentiment signals |
| `hybrid-v1` | Ensemble | Weighted combination of all three strategies |

**Adaptive learning**: Each bot tracks win rates by market conditions (price bucket, BTC momentum, time of day). After every resolved trade, outcomes feed back into a Bayesian learning model that adjusts future decisions. More data = smarter bots.

**Evolution**: Every 12 hours, bots are ranked by P&L. The bottom 2 are replaced with mutated versions of the winners, carrying forward learned knowledge.

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
python dashboard/server.py
# Open http://localhost:8501
```

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
| Max daily loss per bot | $200 SIM | $50 USDC |
| Max daily loss total | $500 SIM | $100 USDC |

## License

MIT
