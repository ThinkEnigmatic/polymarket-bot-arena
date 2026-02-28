# Phantom Swing Bot – Strategy v0.1

Goal: Paper trade a 1,000 USDC account on Solana spot markets (via public price API), with **synthetic leverage up to 20x**, using a transparent, rule-based swing strategy.

This is a **first version** designed to be:
- Simple enough to implement and debug quickly
- Explicit (no black-box ML)
- Reasonably conservative on risk even with a 20x ceiling

---

## 1. Market & Instruments

- Chain: Solana
- Venue: Spot prices via a public API (e.g., Jupiter/Birdeye/Pyth) – no real trades, **paper only**.
- Base currency: USDC
- Initial equity: **1,000 USDC** (paper account)

### 1.1. Tradeable universe (v0.1)

Start with a single, liquid pair:

- **SOL/USDC**

Rationale: high liquidity, continuous trading, clearer trend structure. The code will be written so additional pairs can be added later.

---

## 2. Timeframes & Data

- **Signal timeframe:** 1 hour (1h)
- **Execution/check interval:** 1 minute
- **Lookback window for indicators:** at least 200 1h candles (or equivalent history from the API)

The bot updates every minute, but only makes decisions on the close of each 1h candle.

---

## 3. Indicators

All indicators are computed on the 1h timeframe.

1. **Trend filter:**
   - EMA_fast = 20-period EMA of close
   - EMA_slow = 50-period EMA of close

2. **Volatility (ATR):**
   - ATR_14 = 14-period Average True Range of close/high/low

3. **Range structure:**
   - Recent swing high/low over last N bars (e.g., 20 bars) to define breakout levels

---

## 4. Entry Rules

We only trade **with the prevailing trend** defined by the EMAs.

### 4.1. Long entries

Conditions (all must be true at the close of a 1h candle):

1. **Trend:**
   - EMA_fast > EMA_slow
2. **Price position:**
   - Close price > EMA_fast
3. **Breakout:**
   - Close price > highest close of the last 20 bars (recent swing high)
4. **Volatility sanity:**
   - ATR_14 / Close is between 0.5% and 10% (avoid ultra-low or insane vol)

If all conditions are met and we are not already long, we open a **long** position.

### 4.2. Short entries (v0.1.1)

We now allow **short trades** as a mirror of the long logic.

Conditions (all must be true at the close of a 1h candle):

1. **Trend:**
   - EMA_fast < EMA_slow
2. **Price position:**
   - Close price < EMA_fast
3. **Breakdown:**
   - Close price < lowest close of the last 20 bars (recent swing low)
4. **Volatility sanity:**
   - ATR_14 / Close is between 0.5% and 10% (avoid ultra-low or insane vol)

If all conditions are met and we are not already in a position, we open a **short** position.

---

## 5. Position Sizing & Leverage

Account equity at time of decision: **E** (USDC).

### 5.1. Base risk per trade

- Target risk per trade (if stopped out): **1% of E**

Let:
- `R` = 0.01 * E  (max loss if stop is hit)
- `SL_dist` = distance between entry price and stop-loss price (in USDC)

Then **position size in base currency** (SOL) at 1x would be:

- `size_1x = R / SL_dist`

### 5.2. Stop-loss placement

For long trades:

- Initial stop-loss price = Entry price – `k * ATR_14`
- With k = 1.5 (v0.1)

So:
- `SL_dist = 1.5 * ATR_14`

### 5.3. Synthetic leverage

We allow effective leverage up to 20x but **scale by trend strength and volatility**.

Define a **trend strength score**:

- `TS = (EMA_fast – EMA_slow) / Close`

Heuristic mapping:

- If TS < 0.5%  → max_leverage_for_trade = 1x (weak trend)
- If 0.5% ≤ TS < 1% → max_leverage_for_trade = 3x
- If 1% ≤ TS < 2% → max_leverage_for_trade = 5x
- If TS ≥ 2% → max_leverage_for_trade = 10x (cap v0.1 here, even though global cap is 20x)

We **cap v0.1 at 10x** to avoid insane risk until the strategy is proven.

Effective position size in SOL:

- `size = size_1x * leverage`
- Where `leverage <= max_leverage_for_trade` and is chosen as the max allowed in that band.

---

## 6. Exit Rules

For any open long position:

1. **Stop-loss:**
   - Exit fully if price hits stop-loss price.

2. **Take-profit via R-multiple and trailing:**

   - Compute R-multiple: `(current_price – entry_price) / (entry_price – stop_price)`

   - If R ≥ 1.5:
     - Move stop-loss to breakeven (entry price).

   - If R ≥ 3:
     - Start trailing stop at `trailing_stop = max(trailing_stop, current_price – 2 * ATR_14)`

   - Exit fully when price hits trailing stop.

3. **Trend reversal exit:**

   - If EMA_fast < EMA_slow (trend flips bearish) **and** price closes below EMA_slow, exit at market on the close of that bar (even if stop not hit).

---

## 7. Trade Management & Constraints

1. **One position at a time (v0.1):**
   - Only one SOL/USDC position open at any time.

2. **Max daily loss:**
   - If realized PnL for the current UTC day drops below **–5% of starting equity for that day**, the bot:
     - Closes any open position.
     - Stops opening new trades until the next day.

3. **Max overall drawdown guard (soft):**
   - If equity falls below 800 USDC (–20% from 1,000), the bot logs a warning and can optionally reduce max leverage bands by half.

---

## 8. Logging Requirements

Every **closed trade** must log:

- `timestamp_open` (ISO)
- `timestamp_close` (ISO)
- `pair` (e.g., SOL/USDC)
- `side` (long)
- `entry_price`
- `exit_price`
- `size_base` (SOL)
- `notional_usdc` at entry
- `leverage`
- `initial_stop_price`
- `final_stop_price` (after trailing)
- `pnl_usdc`
- `pnl_pct` (relative to equity at entry)
- `equity_after`
- `reason_exit` (stop, TP, trail, trend_flip)

Additionally, **every 1-minute tick** should log equity to a separate file (or summary), e.g.:

- `timestamp`
- `equity`

---

## 9. Simulation & Paper Trading

- The bot **never sends real orders**. It only simulates fills at the observed prices.
- Slippage/fees (v0.1):
  - Assume fee of 0.1% per trade (roundtrip 0.2%).
  - Slippage is ignored in v0.1 but can be added later.

---

## 10. Future Extensions (not in v0.1, just notes)

- Add more pairs (e.g., WIF/USDC, BONK/USDC) with per-pair risk limits.
- Add basic sentiment filters (funding, open interest proxies) if data is cheap.
- Add short-side logic when trend filter is bearish.
- Add more realistic transaction cost/slippage modeling.

---

This spec is intentionally simple and opinionated so we can implement `bot.py` quickly and iterate on real paper-trade results instead of theory.