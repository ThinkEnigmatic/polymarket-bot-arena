"""FeeZoneMaker — fee-aware directional maker for BTC 5-min markets.

Taker fee formula (from Polymarket Jan 2026 update):
    taker_fee = 0.25 × (price × (1 - price))²

Fee table:
    50¢ → 1.56%   60¢ → 1.44%   65¢ → 1.29%
    70¢ → 1.10%   75¢ → 0.88%   80¢ → 0.64%
    85¢ → 0.43%   90¢ → 0.20%

Strategy:
  Post maker orders in the 60-82¢ YES zone where:
    1. Taker fee is 0.64–1.44% (significant friction for takers crossing us)
    2. Market price gives a clear directional signal (>60¢ = YES favored)
    3. Maker pays ZERO fees — double advantage over takers

We run throughout the full market window (not time-gated), quoting
whenever the price is in the fee-advantage zone. Smaller positions,
higher frequency than LateWindowMaker.

Competing hypothesis:
  Always-on fee-zone quoting beats late-window time-gating because
  price signal alone (no momentum requirement) is sufficient in the
  60-82¢ range, and more trades → more learning data.
"""

import config
import learning
from bots.base_bot import BaseBot

# Fee formula constant — C=1.0 reproduces 1.56% at p=0.5
_FEE_C = 1.0


def taker_fee(price: float) -> float:
    """Simulated Polymarket taker fee: C × 0.25 × (p × (1-p))²."""
    return _FEE_C * 0.25 * (price * (1.0 - price)) ** 2


DEFAULT_PARAMS = {
    "min_price_zone": 0.60,    # Only quote at YES price ≥ 60¢
    "max_price_zone": 0.82,    # Only quote at YES price ≤ 82¢ (above this: too cheap to earn)
    "min_fee_bps": 80,         # Require taker fee ≥ 80 bps at this price to justify quoting
    "spread_ticks": 2,         # Half-spread: 2 ticks (±2¢ around market price)
    "momentum_weight": 0.30,   # Weight of momentum signal in confidence (vs price signal)
    "position_size_pct": 0.06, # 6% of max — smaller per-trade, higher frequency
    "lookback_candles": 5,     # BTC candles for momentum context
    "min_confidence": 0.25,    # Skip if we can't reach this confidence
}


class FeeZoneMakerBot(BaseBot):
    """Quotes YES in the taker-fee-friction zone (60-82¢) throughout the window."""

    strategy_type = "fee_zone_maker"

    def __init__(self, name="fee-zone-maker-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="fee_zone_maker",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def analyze(self, market: dict, signals: dict) -> dict:
        p = self.strategy_params
        market_price = market.get("current_price", 0.5)
        time_rem = market.get("time_remaining_seconds")

        # Maker quote fields — always computed so run_maker_section() can log
        tick = 0.01
        half_spread = p["spread_ticks"] * tick
        maker_bid = round(max(0.01, market_price - half_spread), 2)
        maker_ask = round(min(0.99, market_price + half_spread), 2)
        maker_mid = market_price

        def _hold(reason):
            return {
                "action": "hold",
                "side": "yes",
                "confidence": 0.0,
                "reasoning": reason,
                "maker_bid": maker_bid,
                "maker_ask": maker_ask,
                "maker_mid": maker_mid,
                "maker_side": "both",
            }

        # ── Fee-zone gate ─────────────────────────────────────────────────────
        # Only quote where taker fee gives us meaningful advantage
        min_zone = p["min_price_zone"]
        max_zone = p["max_price_zone"]
        if not (min_zone <= market_price <= max_zone):
            return _hold(
                f"fzm: price {market_price:.2f} outside fee zone [{min_zone},{max_zone}]"
            )

        # Verify taker fee is large enough to justify quoting
        fee = taker_fee(market_price)
        fee_bps = fee * 10000
        min_fee = p["min_fee_bps"]
        if fee_bps < min_fee:
            return _hold(f"fzm: fee {fee_bps:.0f}bps < {min_fee}bps at price={market_price:.2f}")

        # ── BTC momentum context ──────────────────────────────────────────────
        prices = signals.get("prices", [])
        lb = p["lookback_candles"]
        momentum = 0.0
        if len(prices) >= lb and prices[-lb] > 0:
            momentum = (prices[-1] - prices[-lb]) / prices[-lb]

        # Momentum must not contradict the price signal
        # (Price says YES at >60¢; BTC dropping hard is a contradiction)
        if momentum < -0.0015:
            return _hold(f"fzm: BTC momentum contradicts YES zone (mom={momentum:+.5f})")

        # ── Confidence ────────────────────────────────────────────────────────
        # Price signal: how far into the YES zone?  60¢ → 0, 82¢ → 1
        price_signal = (market_price - min_zone) / (max_zone - min_zone)

        # Momentum signal: positive momentum boosts confidence
        mw = p["momentum_weight"]
        mom_boost = min(0.30, max(0.0, momentum * 50))  # up to +0.30 from momentum
        confidence = min(0.88, 0.30 + price_signal * (1.0 - mw) * 0.50 + mom_boost * mw)

        min_conf = p["min_confidence"]
        if confidence < min_conf:
            return _hold(f"fzm: conf {confidence:.3f} < {min_conf}")

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
                f"fzm: price={market_price:.2f} fee={fee_bps:.0f}bps "
                f"mom={momentum:+.5f} psig={price_signal:.2f} conf={confidence:.3f} "
                f"bid={maker_bid:.2f} ask={maker_ask:.2f}"
            ),
            "suggested_amount": amount,
            "features": features,
            "maker_bid": maker_bid,
            "maker_ask": maker_ask,
            "maker_mid": maker_mid,
            "maker_side": "yes",
        }
