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
        """TP bot: always enter, let the 2x exit do the work.

        Never skips a market — every position is an opportunity for the
        0.5s monitor to catch a 2x spike. If 2x never happens, the
        trade resolves at the end of the 5-min window like normal.
        """
        decision = super().make_decision(market, signals)

        if decision.get("action") == "buy":
            decision["reasoning"] += " [TP: monitoring for 2x exit @0.5s]"
            return decision

        # Override skips — this bot always enters a position
        if decision.get("action") == "skip":
            market_price = market.get("current_price", 0.5)
            side = decision.get("side", "yes")
            conf = decision.get("confidence", 0)

            # Still respect market consensus guard (betting against >65c or <35c is suicidal)
            if (market_price > 0.65 and side == "no") or (market_price < 0.35 and side == "yes"):
                return decision

            max_pos = config.get_max_position()
            # Size based on whatever confidence exists, min $1.50
            amount = max(max_pos * 0.03, max_pos * conf * 0.10)
            amount = min(amount, max_pos)

            decision["action"] = "buy"
            decision["suggested_amount"] = amount
            decision["reasoning"] += " [TP override: always enter, monitoring 2x @0.5s]"

        return decision
