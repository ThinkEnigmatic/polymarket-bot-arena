"""Bot 1: Momentum / Trend Following strategy."""

from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "lookback_candles": 5,
    "momentum_threshold": 0.002,  # 0.2% move to trigger
    "position_size_pct": 0.05,    # 5% of max position
    "min_confidence": 0.55,
    "trend_strength_weight": 0.7,
    "volume_weight": 0.3,
}


class MomentumBot(BaseBot):
    def __init__(self, name="momentum-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="momentum",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def analyze(self, market: dict, signals: dict) -> dict:
        """Trade in the direction of short-term price momentum."""
        prices = signals.get("prices", [])
        if len(prices) < self.strategy_params["lookback_candles"]:
            return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "insufficient price data"}

        lookback = self.strategy_params["lookback_candles"]
        recent = prices[-lookback:]
        oldest = recent[0]
        newest = recent[-1]

        if oldest == 0:
            return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "zero price"}

        pct_change = (newest - oldest) / oldest
        threshold = self.strategy_params["momentum_threshold"]

        # Calculate trend strength (consecutive moves in same direction)
        consecutive = 0
        for i in range(1, len(recent)):
            if pct_change > 0 and recent[i] > recent[i-1]:
                consecutive += 1
            elif pct_change < 0 and recent[i] < recent[i-1]:
                consecutive += 1

        trend_strength = consecutive / (len(recent) - 1) if len(recent) > 1 else 0

        # Volume signal (if available)
        volumes = signals.get("volumes", [])
        vol_signal = 0.5
        if len(volumes) >= lookback:
            recent_vol = sum(volumes[-lookback:])
            prev_vol = sum(volumes[-lookback*2:-lookback]) if len(volumes) >= lookback*2 else recent_vol
            vol_signal = min(1.0, recent_vol / max(prev_vol, 1)) * 0.5 + 0.25

        # Combine signals
        tw = self.strategy_params["trend_strength_weight"]
        vw = self.strategy_params["volume_weight"]
        confidence = (trend_strength * tw + vol_signal * vw)

        if abs(pct_change) < threshold:
            return {"action": "hold", "side": "yes", "confidence": confidence,
                    "reasoning": f"momentum {pct_change:.4f} below threshold {threshold}"}

        side = "yes" if pct_change > 0 else "no"
        import config
        amount = config.get_max_position() * self.strategy_params["position_size_pct"]

        return {
            "action": "buy",
            "side": side,
            "confidence": min(confidence, 0.95),
            "reasoning": f"Momentum {pct_change:.4f} ({lookback} candles), trend_str={trend_strength:.2f}, vol={vol_signal:.2f}",
            "suggested_amount": amount,
        }
