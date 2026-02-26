"""Mean Reversion bot with 25% stop-loss.

Because downside is capped at 25%, this bot trades more aggressively:
- Takes 1.5x larger positions (max loss per trade = 37.5% of normal)
- Trades at lower confidence thresholds (0.03 vs 0.06)
- Willing to take marginal edges that a normal bot would skip
"""

import config
from bots.bot_mean_rev import MeanRevBot, DEFAULT_PARAMS


class MeanRevSLBot(MeanRevBot):
    exit_strategy = "stop_loss"
    stop_loss_pct = 0.25

    def __init__(self, name="meanrev-sl25-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        self.strategy_type = "mean_reversion_sl"

    def make_decision(self, market, signals):
        """SL bot: scale up position size since downside is capped at 25%.

        Respects all base class guards (hours filter, NO ban, confidence,
        high-price guard). Only scales up bet size on trades the base logic approves.
        """
        decision = super().make_decision(market, signals)

        if decision.get("action") == "buy":
            # Scale up position size — max loss is 25% of position, not 100%
            amount = decision.get("suggested_amount", 0) * 1.5
            decision["suggested_amount"] = min(amount, config.get_max_position())
            decision["reasoning"] += " [SL: 1.5x size, loss capped 25%]"

        return decision
