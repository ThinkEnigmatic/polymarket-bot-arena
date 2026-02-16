"""Sniper bot — only trades when historical data shows 65%+ win rate.

Data-driven rules from 850+ resolved trades:
- YES at 40-50c: 69% WR, +$34 total (cheap shares, big profit on win)
- YES at 60-80c: 60-87% WR, +$30 total (strong market signal)
- NO at 20-35c: 90% WR, +$14 total (follow strong consensus)
- SKIP 50-60c: 43% WR, -$45 total (coin flip, no edge)
- SKIP 90c+: 44% WR, -$38 total (terrible risk/reward)
- BTC momentum must confirm direction (BTC up → YES only)

Trades less often but with much higher accuracy.
"""

import config
import learning
from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "min_price_yes": 0.40,     # Min YES price for YES bets
    "max_price_yes": 0.85,     # Max YES price for YES bets (above = bad risk/reward)
    "max_price_no": 0.35,      # Max YES price for NO bets (below 35c = bet NO)
    "skip_zone_low": 0.48,     # Start of coin-flip dead zone
    "skip_zone_high": 0.58,    # End of coin-flip dead zone
    "require_momentum": True,  # Only trade when BTC momentum confirms
    "position_size_pct": 0.08, # Larger positions since we're more selective
    "min_confidence": 0.10,    # Only trade with real edge
}


class SniperBot(BaseBot):
    def __init__(self, name="sniper-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="sniper",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )

    def analyze(self, market, signals):
        """Only emit a signal when conditions match high-WR patterns."""
        return {"action": "hold", "side": "yes", "confidence": 0, "reasoning": "sniper: no signal"}

    def make_decision(self, market, signals):
        """Override full decision logic — pure data-driven rules.

        Ignores the base class signal hierarchy. Instead uses simple
        rules derived from historical trade data analysis.
        """
        market_price = market.get("current_price", 0.5)
        p = self.strategy_params

        skip_lo = p.get("skip_zone_low", 0.48)
        skip_hi = p.get("skip_zone_high", 0.58)
        max_yes = p.get("max_price_yes", 0.85)
        min_yes = p.get("min_price_yes", 0.40)
        max_no = p.get("max_price_no", 0.35)
        require_mom = p.get("require_momentum", True)

        # Extract BTC momentum from signals
        prices = signals.get("prices", [])
        btc_momentum = 0.0
        if len(prices) >= 2 and prices[-1] > 0:
            btc_momentum = (prices[-1] - prices[-2]) / prices[-2]

        features = learning.extract_features(market_price, btc_momentum)

        # --- Rule 1: Skip coin-flip zone (50-58c) ---
        if skip_lo <= market_price <= skip_hi:
            return {
                "action": "skip", "side": "yes", "confidence": 0,
                "reasoning": f"sniper: skip coin-flip zone price={market_price:.2f}",
                "suggested_amount": 0, "features": features,
            }

        # --- Rule 2: Skip bad risk/reward zone (>85c for YES) ---
        if market_price > max_yes:
            # YES shares too expensive, profit tiny on win, loss huge on loss
            return {
                "action": "skip", "side": "yes", "confidence": 0,
                "reasoning": f"sniper: skip bad r/r price={market_price:.2f} (>85c)",
                "suggested_amount": 0, "features": features,
            }

        # --- Determine side ---
        side = None
        confidence = 0
        reasoning_parts = [f"price={market_price:.2f}"]

        # YES zone: 40-48c or 58-85c
        if min_yes <= market_price < skip_lo:
            # 40-48c: YES is cheap, 69% WR historically
            side = "yes"
            # Confidence scales with distance from 50c
            confidence = 0.20 + (0.50 - market_price) * 2.0
            reasoning_parts.append(f"cheap-YES zone ({market_price:.0%})")

        elif market_price > skip_hi and market_price <= max_yes:
            # 58-85c: strong market signal, 60-87% WR
            side = "yes"
            confidence = 0.15 + (market_price - 0.50) * 1.5
            reasoning_parts.append(f"strong-YES zone ({market_price:.0%})")

        # NO zone: <35c
        elif market_price <= max_no:
            side = "no"
            confidence = 0.25 + (0.50 - market_price) * 2.0
            reasoning_parts.append(f"strong-NO zone ({market_price:.0%})")

        else:
            # 35-40c: marginal zone, skip
            return {
                "action": "skip", "side": "yes", "confidence": 0,
                "reasoning": f"sniper: marginal zone price={market_price:.2f}",
                "suggested_amount": 0, "features": features,
            }

        # --- Rule 3: BTC momentum must confirm ---
        if require_mom:
            if side == "yes" and btc_momentum < -0.0005:
                # BTC dropping, don't bet YES
                return {
                    "action": "skip", "side": side, "confidence": confidence,
                    "reasoning": f"sniper: BTC momentum negative ({btc_momentum:+.4f}), skip YES",
                    "suggested_amount": 0, "features": features,
                }
            if side == "no" and btc_momentum > 0.0005:
                # BTC rising, don't bet NO
                return {
                    "action": "skip", "side": side, "confidence": confidence,
                    "reasoning": f"sniper: BTC momentum positive ({btc_momentum:+.4f}), skip NO",
                    "suggested_amount": 0, "features": features,
                }

        # --- Learned bias adjustment ---
        prior = 0.50
        learned_bias = learning.get_learned_bias(self.name, features, prior)
        # Slight adjustment from learning (don't let it override data rules)
        if side == "yes" and learned_bias < 0.35:
            confidence *= 0.7  # reduce confidence if learning says NO
        elif side == "no" and learned_bias > 0.65:
            confidence *= 0.7

        confidence = min(0.95, confidence)

        # --- Minimum confidence gate ---
        min_conf = p.get("min_confidence", 0.10)
        if confidence < min_conf:
            return {
                "action": "skip", "side": side, "confidence": confidence,
                "reasoning": f"sniper: conf {confidence:.2f} < {min_conf}",
                "suggested_amount": 0, "features": features,
            }

        # --- Position sizing ---
        max_pos = config.get_max_position()
        size_pct = p.get("position_size_pct", 0.08)
        amount = max_pos * size_pct * (0.5 + confidence)
        amount = min(amount, max_pos)

        mom_str = f"mom={btc_momentum:+.4f}" if btc_momentum != 0 else "mom=flat"
        reasoning_parts.append(mom_str)
        reasoning_parts.append(f"=> {side} conf={confidence:.2f}")

        return {
            "action": "buy",
            "side": side,
            "confidence": confidence,
            "reasoning": "sniper: " + " ".join(reasoning_parts),
            "suggested_amount": amount,
            "features": features,
        }
