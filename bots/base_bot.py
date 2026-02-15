"""Abstract base class all arena bots inherit from."""

import json
import random
import copy
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import db
import learning

logger = logging.getLogger(__name__)


class BaseBot(ABC):
    name: str
    strategy_type: str
    strategy_params: dict
    generation: int
    lineage: str

    # Each strategy type gets a different prior bias.
    # This makes bots differentiated from the start.
    STRATEGY_PRIORS = {
        "momentum": 0.5,        # neutral — learns from momentum signals
        "mean_reversion": 0.5,  # neutral — learns from reversion signals
        "sentiment": 0.5,       # neutral — learns from sentiment
        "hybrid": 0.5,          # neutral — combines learned biases
    }

    def __init__(self, name, strategy_type, params, generation=0, lineage=None):
        self.name = name
        self.strategy_type = strategy_type
        self.strategy_params = params
        self.generation = generation
        self.lineage = lineage or name
        self._paused = False

    @abstractmethod
    def analyze(self, market: dict, signals: dict) -> dict:
        """Analyze market + signals and return a trade signal.

        Returns:
            {
                "action": "buy" | "sell" | "hold",
                "side": "yes" | "no",
                "confidence": 0.0-1.0,
                "reasoning": "why this trade",
                "suggested_amount": float,
            }
        """
        pass

    def make_decision(self, market: dict, signals: dict) -> dict:
        """Make a trading decision using strategy analysis + learned bias.

        This is the main entry point. It:
        1. Gets the strategy's raw analysis
        2. Extracts market features
        3. Queries learned win rates for those features
        4. Combines strategy signal with learned bias
        5. Always returns a trade (never hold)
        """
        # Get strategy's raw signal
        raw_signal = self.analyze(market, signals)

        # Extract features for learning
        market_price = market.get("current_price", 0.5)
        prices = signals.get("prices", [])
        if len(prices) >= 2:
            price_momentum = (prices[-1] - prices[-2]) / prices[-2]
        elif len(prices) == 1 and prices[0] > 0:
            price_momentum = 0.0
        else:
            price_momentum = 0.0

        features = learning.extract_features(market_price, price_momentum)

        # Get learned bias (how much to lean YES based on past outcomes)
        prior = self.STRATEGY_PRIORS.get(self.strategy_type, 0.5)
        learned_yes_bias = learning.get_learned_bias(self.name, features, prior)

        # Combine strategy signal with learned bias
        if raw_signal["action"] != "hold":
            # Strategy has an opinion — weight it 60% strategy, 40% learning
            strategy_yes = 1.0 if raw_signal["side"] == "yes" else 0.0
            strategy_conf = raw_signal["confidence"]
            # Blend: strategy opinion weighted by its confidence + learned bias
            combined_yes = (
                strategy_yes * strategy_conf * 0.6 +
                learned_yes_bias * 0.4 +
                (1 - strategy_conf) * 0.5 * 0.6  # uncertainty pulls to neutral
            )
            reasoning = f"{raw_signal.get('reasoning', '')} | learned_bias={learned_yes_bias:.2f}"
        else:
            # Strategy says hold — rely entirely on learned bias
            combined_yes = learned_yes_bias
            reasoning = f"Learning-driven: yes_bias={learned_yes_bias:.2f}, features={features}"

        # Convert to side + confidence
        side = "yes" if combined_yes > 0.5 else "no"
        confidence = abs(combined_yes - 0.5) * 2  # 0-1 scale
        confidence = max(0.1, min(0.95, confidence))

        # Scale bet size by confidence: high confidence = bigger bet
        max_pos = config.get_max_position()
        min_bet = max_pos * 0.02  # 2% minimum
        max_bet = max_pos * 0.10  # 10% maximum
        amount = min_bet + (max_bet - min_bet) * confidence

        return {
            "action": "buy",
            "side": side,
            "confidence": confidence,
            "reasoning": reasoning,
            "suggested_amount": amount,
            "features": features,  # Pass through for learning updates
        }

    def execute(self, signal: dict, market: dict) -> dict:
        """Place a trade via Simmer SDK based on the signal."""
        if self._paused:
            logger.info(f"[{self.name}] Paused, skipping trade")
            return {"success": False, "reason": "bot_paused"}

        mode = config.get_current_mode()
        venue = config.get_venue()
        max_pos = config.get_max_position()

        # Check risk limits
        daily_loss = db.get_bot_daily_loss(self.name, mode)
        max_daily = config.get_max_daily_loss_per_bot()
        if daily_loss >= max_daily:
            self._paused = True
            logger.warning(f"[{self.name}] Daily loss limit hit (${daily_loss:.2f}), pausing")
            return {"success": False, "reason": "daily_loss_limit"}

        total_daily = db.get_total_daily_loss(mode)
        max_total = config.get_max_daily_loss_total()
        if total_daily >= max_total:
            logger.warning(f"[{self.name}] Total arena daily loss limit hit (${total_daily:.2f})")
            return {"success": False, "reason": "arena_loss_limit"}

        amount = min(signal.get("suggested_amount", max_pos * 0.5), max_pos)

        try:
            if mode == "live":
                return self._execute_live(signal, market, amount, mode)
            else:
                return self._execute_paper(signal, market, amount, venue, mode)

        except Exception as e:
            logger.error(f"[{self.name}] Trade exception: {e}")
            return {"success": False, "reason": str(e)}

    def get_performance(self, hours=12) -> dict:
        """Get bot performance stats."""
        perf = db.get_bot_performance(self.name, hours)
        perf["name"] = self.name
        perf["strategy_type"] = self.strategy_type
        perf["generation"] = self.generation
        perf["paused"] = self._paused
        return perf

    def export_params(self) -> dict:
        return {
            "name": self.name,
            "strategy_type": self.strategy_type,
            "generation": self.generation,
            "lineage": self.lineage,
            "params": copy.deepcopy(self.strategy_params),
        }

    def mutate(self, winning_params: dict, mutation_rate: float = None) -> dict:
        """Create mutated params from winning bot's params."""
        rate = mutation_rate or config.MUTATION_RATE
        new_params = copy.deepcopy(winning_params)

        numeric_keys = [k for k, v in new_params.items() if isinstance(v, (int, float))]
        num_mutations = min(random.randint(2, 3), len(numeric_keys))
        keys_to_mutate = random.sample(numeric_keys, num_mutations) if numeric_keys else []

        for key in keys_to_mutate:
            val = new_params[key]
            delta = val * random.uniform(-rate, rate)
            new_val = val + delta
            if isinstance(val, int):
                new_params[key] = max(1, int(new_val))
            else:
                new_params[key] = max(0.01, round(new_val, 4))

        return new_params

    def reset_daily(self):
        """Reset daily pause state."""
        self._paused = False

    def _execute_paper(self, signal, market, amount, venue, mode):
        """Execute via Simmer (paper trading)."""
        import requests
        api_key = self._load_api_key()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        payload = {
            "market_id": market.get("id") or market.get("market_id"),
            "side": signal["side"],
            "amount": amount,
            "venue": venue,
            "source": f"arena:{self.name}",
            "reasoning": signal.get("reasoning", ""),
        }

        resp = requests.post(
            f"{config.SIMMER_BASE_URL}/api/sdk/trade",
            headers=headers, json=payload, timeout=30
        )

        if resp.status_code in (200, 201):
            result = resp.json()
            db.log_trade(
                bot_name=self.name,
                market_id=market.get("id") or market.get("market_id"),
                market_question=market.get("question"),
                side=signal["side"],
                amount=amount,
                venue=venue,
                mode=mode,
                confidence=signal["confidence"],
                reasoning=signal.get("reasoning"),
                trade_id=result.get("trade_id"),
                shares_bought=result.get("shares_bought"),
            )
            logger.info(f"[{self.name}] Paper trade: {signal['side']} ${amount:.2f} on {market.get('question', '')[:50]}")
            return {"success": True, "trade_id": result.get("trade_id")}
        else:
            logger.error(f"[{self.name}] Paper trade failed: {resp.status_code} {resp.text[:200]}")
            return {"success": False, "reason": f"api_error_{resp.status_code}"}

    def _execute_live(self, signal, market, amount, mode):
        """Execute directly on Polymarket CLOB (live trading)."""
        import polymarket_client

        side = signal["side"].lower()
        if side == "yes":
            token_id = market.get("polymarket_token_id")
        else:
            token_id = market.get("polymarket_no_token_id")

        if not token_id:
            logger.error(f"[{self.name}] No token ID for side={side} on {market.get('question', '')[:50]}")
            return {"success": False, "reason": "missing_token_id"}

        result = polymarket_client.place_market_order(
            token_id=token_id,
            side=side,
            amount=amount,
        )

        if result.get("success"):
            db.log_trade(
                bot_name=self.name,
                market_id=market.get("id") or market.get("market_id"),
                market_question=market.get("question"),
                side=signal["side"],
                amount=amount,
                venue="polymarket",
                mode=mode,
                confidence=signal["confidence"],
                reasoning=signal.get("reasoning"),
                trade_id=result.get("order_id"),
                shares_bought=result.get("size"),
            )
            logger.info(f"[{self.name}] LIVE trade: {signal['side']} ${amount} at {result.get('price')} on {market.get('question', '')[:50]}")
        else:
            logger.error(f"[{self.name}] LIVE trade failed: {result.get('error')}")

        return result

    def _load_api_key(self):
        import json as _json
        with open(config.SIMMER_API_KEY_PATH) as f:
            return _json.load(f).get("api_key")
