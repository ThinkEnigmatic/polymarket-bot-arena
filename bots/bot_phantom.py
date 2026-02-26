"""Phantom Swing bot — adapted from strategy.md for 5-min binary markets.

Uses EMA trend filter and breakout logic to trade in the direction of momentum.
"""

import math
import config
from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "atr_period": 14,
    "breakout_lookback": 20,
    "min_atr_pct": 0.0005,    # 0.05%
    "max_atr_pct": 0.01,      # 1.0%
    "position_size_pct": 0.06,
    "min_confidence": 0.20,
}


class PhantomBot(BaseBot):
    def __init__(self, name="phantom-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="hybrid", # Using hybrid type for similar signal weight
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        self.strategy_type = "phantom"

    def _calc_ema(self, prices, period):
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0
        
        alpha = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = (p * alpha) + (ema * (1 - alpha))
        return ema

    def _calc_atr(self, prices, period):
        """Simple ATR approximation using close prices since we don't have H/L."""
        if len(prices) < period + 1:
            return 0
        
        diffs = [abs(prices[i] - prices[i-1]) for i in range(len(prices)-period, len(prices))]
        return sum(diffs) / period

    def analyze(self, market: dict, signals: dict) -> dict:
        """Swing strategy: follow the trend defined by EMAs and breakouts."""
        prices = signals.get("prices", [])
        p = self.strategy_params
        
        if len(prices) < p["ema_slow"] + p["breakout_lookback"]:
            return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "insufficient data"}

        current_price = signals.get("latest", prices[-1])
        
        # 1. Trend Filter
        ema_fast = self._calc_ema(prices, p["ema_fast"])
        ema_slow = self._calc_ema(prices, p["ema_slow"])
        
        # 2. Breakout
        recent_window = prices[-p["breakout_lookback"]:]
        recent_high = max(recent_window)
        recent_low = min(recent_window)
        
        # 3. Volatility (ATR)
        atr = self._calc_atr(prices, p["atr_period"])
        atr_pct = atr / current_price if current_price > 0 else 0
        
        # Volatility sanity check
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            return {
                "action": "hold", "side": "yes", "confidence": 0, 
                "reasoning": f"phantom: vol out of bounds ({atr_pct:.4%})"
            }

        # Long Entry (Bullish)
        if ema_fast > ema_slow and current_price > ema_fast and current_price > recent_high:
            trend_strength = (ema_fast - ema_slow) / current_price
            confidence = 0.3 + min(0.4, trend_strength * 100)
            return {
                "action": "buy",
                "side": "yes",
                "confidence": confidence,
                "reasoning": f"phantom LONG: trend={trend_strength:.4%}, breakout above {recent_high:.0f}",
                "suggested_amount": config.get_max_position() * p["position_size_pct"]
            }

        # Short Entry (Bearish)
        if ema_fast < ema_slow and current_price < ema_fast and current_price < recent_low:
            trend_strength = (ema_slow - ema_fast) / current_price
            confidence = 0.3 + min(0.4, trend_strength * 100)
            return {
                "action": "buy",
                "side": "no",
                "confidence": confidence,
                "reasoning": f"phantom SHORT: trend={trend_strength:.4%}, breakdown below {recent_low:.0f}",
                "suggested_amount": config.get_max_position() * p["position_size_pct"]
            }

        return {
            "action": "hold", "side": "yes", "confidence": 0,
            "reasoning": f"phantom: no signal (ema_f={ema_fast:.0f}, ema_s={ema_slow:.0f}, high={recent_high:.0f}, low={recent_low:.0f})"
        }
