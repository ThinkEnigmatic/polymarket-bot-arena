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
        """SL bot: more aggressive entries since downside is capped at 25%.

        With a 25% stop-loss, max loss per trade is only 25% of the position
        instead of 100%. This changes the risk/reward math:
        - A trade with 40% win probability at even odds is -EV normally
        - But with SL capping losses at 25%, it becomes +EV
        So we trade more aggressively and size up.
        """
        decision = super().make_decision(market, signals)

        if decision.get("action") == "buy":
            # Scale up position size — max loss is 25% of position, not 100%
            # So a $6 trade can only lose $1.50 (same risk as normal $1.50 trade)
            amount = decision.get("suggested_amount", 0) * 1.5
            decision["suggested_amount"] = min(amount, config.get_max_position())
            decision["reasoning"] += " [SL: 1.5x size, loss capped 25%]"
            return decision

        if decision.get("action") == "skip":
            conf = decision.get("confidence", 0)
            # Take marginal trades that base bot would skip —
            # at 25% SL the risk is bounded so small edges are worth taking
            if conf >= 0.03:
                market_price = market.get("current_price", 0.5)
                side = decision.get("side", "yes")
                # Still respect market consensus guard
                if (market_price > 0.65 and side == "no") or (market_price < 0.35 and side == "yes"):
                    return decision
                max_pos = config.get_max_position()
                decision["action"] = "buy"
                decision["suggested_amount"] = max_pos * 0.05  # small size for marginal trades
                decision["reasoning"] += " [SL override: marginal edge, loss capped]"

        return decision
