"""Bot 2: Mean Reversion strategy."""

import math
from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "lookback_candles": 20,
    "bb_std_dev": 2.0,         # Bollinger Band width
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "reversion_threshold": 0.6, # z-score threshold
    "position_size_pct": 0.05,
    "min_confidence": 0.55,
}


class MeanRevBot(BaseBot):
    def __init__(self, name="meanrev-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="mean_reversion",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def _calc_rsi(self, prices, period):
        if len(prices) < period + 1:
            return 50  # neutral
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i-1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        gains = gains[-period:]
        losses = losses[-period:]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calc_zscore(self, prices, lookback):
        if len(prices) < lookback:
            return 0
        window = prices[-lookback:]
        mean = sum(window) / len(window)
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        std = math.sqrt(variance) if variance > 0 else 1
        return (prices[-1] - mean) / std

    def analyze(self, market: dict, signals: dict) -> dict:
        """Bet against overextended moves."""
        prices = signals.get("prices", [])
        lookback = self.strategy_params["lookback_candles"]

        if len(prices) < lookback:
            return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "insufficient data"}

        # Z-score: how far price is from recent mean
        zscore = self._calc_zscore(prices, lookback)

        # RSI: momentum oscillator
        rsi = self._calc_rsi(prices, self.strategy_params["rsi_period"])

        threshold = self.strategy_params["reversion_threshold"]

        # Overextended UP → bet NO (expect reversion down)
        if zscore > threshold and rsi > self.strategy_params["rsi_overbought"]:
            confidence = min(0.95, 0.5 + abs(zscore) * 0.15 + (rsi - 70) * 0.005)
            import config
            amount = config.get_max_position() * self.strategy_params["position_size_pct"]
            return {
                "action": "buy",
                "side": "no",
                "confidence": confidence,
                "reasoning": f"Mean reversion SHORT: z={zscore:.2f}, RSI={rsi:.1f} (overbought)",
                "suggested_amount": amount,
            }

        # Overextended DOWN → bet YES (expect reversion up)
        if zscore < -threshold and rsi < self.strategy_params["rsi_oversold"]:
            confidence = min(0.95, 0.5 + abs(zscore) * 0.15 + (30 - rsi) * 0.005)
            import config
            amount = config.get_max_position() * self.strategy_params["position_size_pct"]
            return {
                "action": "buy",
                "side": "yes",
                "confidence": confidence,
                "reasoning": f"Mean reversion LONG: z={zscore:.2f}, RSI={rsi:.1f} (oversold)",
                "suggested_amount": amount,
            }

        return {
            "action": "hold", "side": "yes", "confidence": 0,
            "reasoning": f"No reversion signal: z={zscore:.2f}, RSI={rsi:.1f}"
        }
