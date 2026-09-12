"""Legacy mean-reversion variant that holds positions to resolution.

The former stop-loss only changed local database P&L; it did not sell the
venue position. It is disabled until a real exit-order path is implemented.
"""

from bots.bot_mean_rev import MeanRevBot, DEFAULT_PARAMS


class MeanRevSLBot(MeanRevBot):
    exit_strategy = None
    stop_loss_pct = 0.0

    def __init__(self, name="meanrev-sl25-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        self.strategy_type = "mean_reversion_sl"

    def make_decision(self, market, signals):
        """Use normal sizing and hold to resolution."""
        return super().make_decision(market, signals)
