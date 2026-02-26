"""LateWindowMaker — models the article's "T-10s maker" strategy.

The article:
  "At T-10 seconds before window close, BTC direction is ~85% determined.
   Post a maker order on the winning side at 90-95¢."

We use a 90-second entry window because Simmer polling runs every 15s and
the bot needs reaction time. At T-90s conviction is ~70%; the trade-off vs
LateWindowMaker's peer (FeeZoneMaker) is: fewer trades, higher WR target.

Paper mode: Simmer has no limit-order book, so we execute at market price
like all other bots. The "maker" logic controls WHEN and IF we enter.
Logs theoretical maker metrics (what limit we'd post, edge in bps) so we
can compare against real maker results if this ever goes live.

Competing hypothesis:
  High-conviction, time-gated, momentum-confirmed entries beat
  always-on fee-zone bets because the signal is strongest in the final seconds.
"""

import config
import learning
from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "entry_window_sec": 90,    # Only activate in the last 90 seconds of a market
    "min_momentum": 0.0008,    # Require |BTC momentum| ≥ 0.08% (avg over lookback)
    "min_price_yes": 0.58,     # Market price must be ≥ 58¢ to confirm YES direction
    "max_price_yes": 0.92,     # Cap: above 92¢ profit margin is too thin
    "maker_offset_pct": 0.06,  # Simulated limit = market_price + 6¢ (captures spread)
    "position_size_pct": 0.10, # 10% of max — large because entries are highly selective
    "lookback_candles": 3,     # BTC candles used for momentum calculation
}


class LateWindowMakerBot(BaseBot):
    """Posts directional YES in the final 90s when BTC momentum and price align."""

    strategy_type = "late_window_maker"

    def __init__(self, name="late-window-maker-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="late_window_maker",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def analyze(self, market: dict, signals: dict) -> dict:
        p = self.strategy_params
        time_rem = market.get("time_remaining_seconds")
        market_price = market.get("current_price", 0.5)

        # Maker quote fields always returned so run_maker_section() can log them
        def _hold(reason):
            return {
                "action": "hold",
                "side": "yes",
                "confidence": 0.0,
                "reasoning": reason,
                "maker_bid": round(max(0.01, market_price - 0.02), 2),
                "maker_ask": round(min(0.99, market_price + 0.02), 2),
                "maker_mid": market_price,
                "maker_side": "both",
            }

        # ── Time gate ────────────────────────────────────────────────────────
        entry_window = p["entry_window_sec"]
        if time_rem is None or time_rem > entry_window:
            return _hold(f"lwm: waiting (rem={time_rem}s, window={entry_window}s)")

        # ── BTC momentum ─────────────────────────────────────────────────────
        prices = signals.get("prices", [])
        lb = p["lookback_candles"]
        momentum = 0.0
        if len(prices) >= lb and prices[-lb] > 0:
            momentum = (prices[-1] - prices[-lb]) / prices[-lb]

        min_mom = p["min_momentum"]
        if abs(momentum) < min_mom:
            return _hold(f"lwm: weak momentum ({momentum:+.5f} < {min_mom})")

        # ── NO ban ───────────────────────────────────────────────────────────
        # Data: NO bets 44% WR all-time. Don't trade NO even with downward momentum.
        if momentum < 0:
            return _hold(f"lwm: NO side banned (mom={momentum:+.5f})")

        # ── Market price confirmation ────────────────────────────────────────
        min_price = p["min_price_yes"]
        max_price = p["max_price_yes"]

        if market_price < min_price:
            return _hold(f"lwm: price {market_price:.2f} < {min_price} (no YES confirmation)")
        if market_price > max_price:
            return _hold(f"lwm: price {market_price:.2f} > {max_price} (margin too thin)")

        # ── Maker quote computation ───────────────────────────────────────────
        # What we'd post as a limit order: slightly ahead of market to capture spread
        maker_ask = round(min(max_price, market_price + p["maker_offset_pct"]), 2)
        maker_bid = round(max(0.01, market_price - 0.02), 2)
        maker_mid = round((maker_bid + maker_ask) / 2, 3)
        edge_bps = p["maker_offset_pct"] * 10000  # spread captured if filled

        # ── Confidence: urgency × momentum strength ───────────────────────────
        time_weight = 1.0 - (time_rem / entry_window)  # 0 at window-open, 1 at close
        mom_strength = min(1.0, abs(momentum) / (min_mom * 5))
        confidence = min(0.92, 0.45 + time_weight * 0.30 + mom_strength * 0.20)

        # ── Features ─────────────────────────────────────────────────────────
        of_data = signals.get("orderflow", {})
        features = learning.extract_features(
            market_price, momentum,
            volume=of_data.get("volume_24h"),
            time_rem=time_rem,
        )

        amount = config.get_max_position() * p["position_size_pct"]

        return {
            "action": "buy",
            "side": "yes",
            "confidence": confidence,
            "reasoning": (
                f"lwm: time={time_rem:.0f}s mom={momentum:+.5f} "
                f"price={market_price:.2f} limit={maker_ask:.2f} "
                f"edge={edge_bps:.0f}bps tw={time_weight:.2f}"
            ),
            "suggested_amount": amount,
            "features": features,
            "maker_bid": maker_bid,
            "maker_ask": maker_ask,
            "maker_mid": maker_mid,
            "maker_side": "yes",
        }
