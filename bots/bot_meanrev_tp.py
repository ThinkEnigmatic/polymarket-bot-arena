"""Legacy take-profit variant that now holds positions to resolution.

The former take-profit updated only local accounting without selling the venue
position. It is disabled until venue-backed exits are implemented.
"""

from bots.bot_mean_rev import MeanRevBot, DEFAULT_PARAMS


class MeanRevTPBot(MeanRevBot):
    exit_strategy = None
    take_profit_pct = 0.0

    def __init__(self, name="meanrev-tp2x-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        self.strategy_type = "mean_reversion_tp"

    def make_decision(self, market, signals):
        """Use normal entry logic and hold to resolution."""
        return super().make_decision(market, signals)
