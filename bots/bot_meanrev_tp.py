"""Mean Reversion bot with 2x take-profit via intra-window tick tracking.

This bot ALWAYS opens a position on every market. The position monitor
polls Simmer every 0.5s and closes the position if it ever reaches
100% profit (2x the initial bet). If it never hits 2x, the position
holds until the trading window closes and resolves normally.

Entry logic: same mean-reversion signals, but NEVER skips a market.
Exit logic: early close at 2x via PositionMonitorThread, otherwise hold.
"""

import config
from bots.bot_mean_rev import MeanRevBot, DEFAULT_PARAMS


class MeanRevTPBot(MeanRevBot):
    exit_strategy = "take_profit"
    take_profit_pct = 1.0  # 100% = 2x the initial bet

    def __init__(self, name="meanrev-tp2x-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        self.strategy_type = "mean_reversion_tp"

    def make_decision(self, market, signals):
        """TP bot: enter when base logic says buy, monitor for 2x exit.

        Respects all base class guards (hours filter, NO ban, confidence,
        high-price guard). Only adds TP monitoring annotation.
        """
        decision = super().make_decision(market, signals)

        if decision.get("action") == "buy":
            decision["reasoning"] += " [TP: monitoring for 2x exit @0.5s]"

        return decision
