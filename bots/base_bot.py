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

    # Exit strategy: None = hold to resolution (default)
    # "stop_loss" = exit when position is down stop_loss_pct
    # "take_profit" = exit when position is up take_profit_pct
    exit_strategy: str = None
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0

    # Each strategy type gets different parameters for differentiation.
    # This creates real competition for evolution to select from.
    STRATEGY_PRIORS = {
        "momentum": 0.52,       # slight YES bias — momentum tends bullish
        "mean_reversion": 0.48, # slight NO bias — mean reversion bets against crowd
        "mean_reversion_sl": 0.48,
        "mean_reversion_tp": 0.48,
        "sniper": 0.50,         # neutral — sniper uses its own rules
        "sentiment": 0.50,      # neutral
        "hybrid": 0.50,         # neutral
    }
    # How aggressively each strategy trusts the market price signal
    MARKET_PRICE_AGGRESSION = {
        "momentum": 1.2,        # follows market price strongly
        "mean_reversion": 0.95, # nearly follows market (contrarian was -$16 loser)
        "mean_reversion_sl": 0.95,
        "mean_reversion_tp": 0.95,
        "sniper": 1.0,          # sniper overrides make_decision entirely
        "sentiment": 1.0,       # neutral
        "hybrid": 1.0,          # neutral (was 0.9, contrarian loses)
    }
    # Minimum confidence to place a trade (low = trades more, generates learning data)
    MIN_TRADE_CONFIDENCE = {
        "momentum": 0.01,       # trades almost everything (aggressive learner)
        "mean_reversion": 0.06, # slightly selective
        "mean_reversion_sl": 0.06,
        "mean_reversion_tp": 0.06,
        "sniper": 0.10,         # highly selective — only trades high-WR zones
        "sentiment": 0.03,      # moderate
        "hybrid": 0.05,         # moderate-selective
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
        """Make a trading decision using market price edge + strategy + learning.

        Signal hierarchy:
        1. Market price edge (strongest — when price is far from 50c, follow it)
        2. BTC momentum (if price is moving, lean that direction)
        3. Strategy analysis (adds differentiation between bots)
        4. Learned bias (accumulates over time, adjusts everything)

        Skips trades when confidence is too low (no edge = no bet).
        """
        market_price = market.get("current_price", 0.5)

        # --- Signal 1: Market price edge ---
        # When YES is priced high, YES usually wins. The further from 50c, the stronger.
        aggression = self.MARKET_PRICE_AGGRESSION.get(self.strategy_type, 1.0)
        price_edge = (market_price - 0.5) * aggression
        # price_edge > 0 means lean YES, < 0 means lean NO

        # --- Signal 2: BTC momentum ---
        prices = signals.get("prices", [])
        btc_latest = signals.get("latest", 0)
        price_momentum = 0.0
        if len(prices) >= 2 and prices[-1] > 0:
            price_momentum = (prices[-1] - prices[-2]) / prices[-2]
        elif btc_latest > 0 and len(prices) >= 1 and prices[-1] > 0:
            # Use live price vs last closed candle
            price_momentum = (btc_latest - prices[-1]) / prices[-1]
        elif btc_latest > 0 and len(prices) == 0:
            # No candles yet — use market price direction as weak proxy
            # Market price > 0.5 suggests BTC trending up in this window
            price_momentum = (market_price - 0.5) * 0.005
        # Momentum signal: BTC going up → lean YES
        momentum_signal = max(-0.15, min(0.15, price_momentum * 30))

        # --- Signal 3: Strategy analysis ---
        raw_signal = self.analyze(market, signals)
        strategy_signal = 0.0
        if raw_signal["action"] != "hold":
            strategy_yes = 1.0 if raw_signal["side"] == "yes" else -1.0
            strategy_signal = strategy_yes * raw_signal["confidence"] * 0.15

        # --- Signal 4: Learning bias ---
        features = learning.extract_features(market_price, price_momentum)
        prior = self.STRATEGY_PRIORS.get(self.strategy_type, 0.5)
        learned_yes_bias = learning.get_learned_bias(self.name, features, prior)
        # Convert from 0-1 to -0.5 to +0.5
        learning_signal = (learned_yes_bias - 0.5)

        # Dynamic learning weight: ramps up as bot accumulates data
        perf = db.get_bot_performance(self.name, hours=168)
        total_resolved = perf.get("total_trades", 0)
        learning_weight = min(0.6, 0.1 + total_resolved * 0.01)

        # --- Combine all signals ---
        combined = (
            price_edge * 0.50 +           # Market price is primary signal
            momentum_signal * 0.20 +       # BTC momentum is secondary
            strategy_signal * 0.15 +       # Strategy adds differentiation
            learning_signal * learning_weight  # Learning grows over time
        )
        # combined > 0 → YES, < 0 → NO

        side = "yes" if combined > 0 else "no"
        confidence = min(0.95, abs(combined) * 2)

        # --- Market consensus guard ---
        # Data shows: betting against strong market consensus is 0-10% WR.
        # When market strongly favors one side, NEVER bet against it.
        if market_price > 0.65 and side == "no":
            return {
                "action": "skip",
                "side": side,
                "confidence": confidence,
                "reasoning": f"Market consensus guard: price={market_price:.2f} too high to bet NO",
                "suggested_amount": 0,
                "features": features,
            }
        if market_price < 0.35 and side == "yes":
            return {
                "action": "skip",
                "side": side,
                "confidence": confidence,
                "reasoning": f"Market consensus guard: price={market_price:.2f} too low to bet YES",
                "suggested_amount": 0,
                "features": features,
            }

        # --- Skip low-confidence trades (no edge = no bet) ---
        min_conf = self.MIN_TRADE_CONFIDENCE.get(self.strategy_type, 0.08)
        if confidence < min_conf:
            return {
                "action": "skip",
                "side": side,
                "confidence": confidence,
                "reasoning": f"No edge: conf={confidence:.3f} < {min_conf} | price={market_price:.2f}",
                "suggested_amount": 0,
                "features": features,
            }

        # --- Bet sizing: proportional to edge strength ---
        # Data shows: conf 0.30-0.50 is the sweet spot (67.9% WR, +$48).
        # conf >0.50 drops to 48.6% WR but bets are bigger → big losses.
        # Cap bet-sizing confidence at 0.45 to stay in the profitable zone.
        bet_conf = min(confidence, 0.45)
        max_pos = config.get_max_position()
        if bet_conf > 0.2:
            # Moderate-to-strong edge
            amount = max_pos * (0.05 + bet_conf * 0.10)
        else:
            # Weak edge — small bet (still generates learning data)
            amount = max_pos * 0.03

        reasoning = (
            f"price={market_price:.2f} edge={price_edge:+.3f} "
            f"mom={momentum_signal:+.3f} strat={strategy_signal:+.3f} "
            f"learn={learning_signal:+.3f}(w={learning_weight:.0%}) "
            f"=> {side} conf={confidence:.2f}"
        )

        return {
            "action": "buy",
            "side": side,
            "confidence": confidence,
            "reasoning": reasoning,
            "suggested_amount": amount,
            "features": features,
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
                trade_features=signal.get("features"),
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
        # Try per-bot key first, then fall back to default
        try:
            with open(config.SIMMER_BOT_KEYS_PATH) as f:
                bot_keys = _json.load(f)
            if self.name in bot_keys:
                return bot_keys[self.name]
            # Check by slot assignment (for evolved bots inheriting a slot)
            if hasattr(self, '_api_key_slot') and self._api_key_slot in bot_keys:
                return bot_keys[self._api_key_slot]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # Fallback: default key
        with open(config.SIMMER_API_KEY_PATH) as f:
            return _json.load(f).get("api_key")
