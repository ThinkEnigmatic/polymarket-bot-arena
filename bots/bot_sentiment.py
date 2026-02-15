"""Bot 3: Sentiment-based strategy using X/social signals."""

from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "sentiment_window_min": 5,
    "bullish_threshold": 0.6,
    "bearish_threshold": 0.4,
    "influencer_weight": 2.0,
    "noise_filter_min_posts": 5,
    "position_size_pct": 0.04,
    "min_confidence": 0.55,
    "sentiment_momentum_weight": 0.6,
    "raw_sentiment_weight": 0.4,
}


class SentimentBot(BaseBot):
    def __init__(self, name="sentiment-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="sentiment",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def analyze(self, market: dict, signals: dict) -> dict:
        """Trade based on X/social sentiment for BTC/SOL."""
        sentiment_data = signals.get("sentiment", {})

        if not sentiment_data:
            return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "no sentiment data"}

        # Sentiment data expected format:
        # {
        #   "score": 0.0-1.0 (0=bearish, 0.5=neutral, 1=bullish),
        #   "post_count": int,
        #   "influencer_score": 0.0-1.0,
        #   "momentum": float (change in sentiment over window),
        # }

        score = sentiment_data.get("score", 0.5)
        post_count = sentiment_data.get("post_count", 0)
        influencer_score = sentiment_data.get("influencer_score", 0.5)
        momentum = sentiment_data.get("momentum", 0)

        # Filter noise: need minimum posts to trust the signal
        if post_count < self.strategy_params["noise_filter_min_posts"]:
            return {"action": "hold", "side": "yes", "confidence": 0,
                    "reasoning": f"too few posts ({post_count}) for reliable signal"}

        # Weight influencer sentiment higher
        weighted_score = (
            score + (influencer_score - 0.5) * self.strategy_params["influencer_weight"]
        ) / (1 + self.strategy_params["influencer_weight"] * 0.5)
        weighted_score = max(0, min(1, weighted_score))

        # Combine raw sentiment with momentum
        sw = self.strategy_params["raw_sentiment_weight"]
        mw = self.strategy_params["sentiment_momentum_weight"]
        # Momentum > 0 means sentiment is improving
        momentum_signal = 0.5 + momentum * 5  # scale momentum
        momentum_signal = max(0, min(1, momentum_signal))

        combined = weighted_score * sw + momentum_signal * mw

        bullish_thresh = self.strategy_params["bullish_threshold"]
        bearish_thresh = self.strategy_params["bearish_threshold"]

        if combined > bullish_thresh:
            confidence = min(0.95, 0.5 + (combined - bullish_thresh) * 2)
            import config
            amount = config.get_max_position() * self.strategy_params["position_size_pct"]
            return {
                "action": "buy",
                "side": "yes",
                "confidence": confidence,
                "reasoning": f"Bullish sentiment: score={score:.2f}, influencer={influencer_score:.2f}, momentum={momentum:.3f}, posts={post_count}",
                "suggested_amount": amount,
            }

        if combined < bearish_thresh:
            confidence = min(0.95, 0.5 + (bearish_thresh - combined) * 2)
            import config
            amount = config.get_max_position() * self.strategy_params["position_size_pct"]
            return {
                "action": "buy",
                "side": "no",
                "confidence": confidence,
                "reasoning": f"Bearish sentiment: score={score:.2f}, influencer={influencer_score:.2f}, momentum={momentum:.3f}, posts={post_count}",
                "suggested_amount": amount,
            }

        return {
            "action": "hold", "side": "yes", "confidence": 0,
            "reasoning": f"Neutral sentiment: combined={combined:.2f}"
        }
