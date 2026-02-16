"""Bot Arena Manager — runs 4 competing bots with 2-hour evolution cycles."""

import argparse
import json
import logging
import sys
import time
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import db
import learning
from bots.bot_momentum import MomentumBot
from bots.bot_mean_rev import MeanRevBot
from bots.bot_sentiment import SentimentBot
from bots.bot_hybrid import HybridBot
from bots.bot_meanrev_sl import MeanRevSLBot
from bots.bot_meanrev_tp import MeanRevTPBot
from bots.bot_sniper import SniperBot
from signals.price_feed import get_feed as get_price_feed
from signals.sentiment import get_feed as get_sentiment_feed
from signals.orderflow import get_feed as get_orderflow_feed
from copytrading.tracker import WalletTracker
from copytrading.copier import TradeCopier

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_DIR / "arena.log"),
    ]
)
logger = logging.getLogger("arena")

# Market check interval (seconds)
TRADE_INTERVAL = 15    # Discover markets + place trades every 15s (fast market discovery)
RESOLVE_INTERVAL = 60  # Resolve trades + expire stale every 60s (expensive, no need to rush)
FAST_POLL_INTERVAL = 0.5  # Poll market prices for SL/TP exits every 0.5s


def create_default_bots():
    """Create the 4 bots from active DB configs (or defaults for first run)."""
    active = db.get_active_bots()
    if active:
        bot_classes = {
            "momentum": MomentumBot,
            "mean_reversion": MeanRevBot,
            "mean_reversion_sl": MeanRevSLBot,
            "mean_reversion_tp": MeanRevTPBot,
            "sniper": SniperBot,
            "sentiment": SentimentBot,
            "hybrid": HybridBot,
        }
        bots = []
        for cfg in active:
            cls = bot_classes.get(cfg["strategy_type"], MomentumBot)
            params = cfg["params"]
            if isinstance(params, str):
                import json as _j
                params = _j.loads(params)
            bots.append(cls(
                name=cfg["bot_name"],
                params=params,
                generation=cfg["generation"],
                lineage=cfg.get("lineage"),
            ))
        if bots:
            return bots

    # First run fallback
    return [
        MomentumBot(name="momentum-v1", generation=0),
        HybridBot(name="hybrid-v1", generation=0),
        MeanRevSLBot(name="meanrev-sl25-v1", generation=0),
        MeanRevTPBot(name="meanrev-tp2x-v1", generation=0),
    ]


def create_evolved_bot(winner, loser_type, gen_number):
    """Create an evolved bot based on the winner's influence + loser's strategy.

    Uses the loser strategy's DEFAULT params as a base, copies over any
    shared keys from the winner (e.g. lookback_candles, position_size_pct),
    then mutates. This prevents KeyError when winner and loser have
    different param schemas.
    """
    from bots.bot_momentum import DEFAULT_PARAMS as MOMENTUM_DEFAULTS
    from bots.bot_mean_rev import DEFAULT_PARAMS as MEANREV_DEFAULTS
    from bots.bot_hybrid import DEFAULT_PARAMS as HYBRID_DEFAULTS
    from bots.bot_sentiment import DEFAULT_PARAMS as SENTIMENT_DEFAULTS

    from bots.bot_sniper import DEFAULT_PARAMS as SNIPER_DEFAULTS

    bot_classes = {
        "momentum": MomentumBot,
        "mean_reversion": MeanRevBot,
        "mean_reversion_sl": MeanRevSLBot,
        "mean_reversion_tp": MeanRevTPBot,
        "sniper": SniperBot,
        "sentiment": SentimentBot,
        "hybrid": HybridBot,
    }

    default_params_map = {
        "momentum": MOMENTUM_DEFAULTS,
        "mean_reversion": MEANREV_DEFAULTS,
        "mean_reversion_sl": MEANREV_DEFAULTS,
        "mean_reversion_tp": MEANREV_DEFAULTS,
        "sniper": SNIPER_DEFAULTS,
        "sentiment": SENTIMENT_DEFAULTS,
        "hybrid": HYBRID_DEFAULTS,
    }

    # Start with the target strategy's defaults
    base_params = default_params_map.get(loser_type, MOMENTUM_DEFAULTS).copy()

    # Copy shared keys from winner (transfers learned tuning for common params)
    winner_params = winner.export_params()["params"]
    for key in base_params:
        if key in winner_params:
            base_params[key] = winner_params[key]

    # Mutate
    new_params = winner.mutate(base_params)
    name = f"{loser_type}-g{gen_number}-{random.randint(100,999)}"

    cls = bot_classes.get(loser_type, MomentumBot)
    return cls(
        name=name,
        params=new_params,
        generation=gen_number,
        lineage=f"{winner.name} -> {name}",
    )


def _validate_bot(bot):
    """Smoke-test a bot by running make_decision with dummy data.
    Returns True if bot can trade, False if it crashes."""
    dummy_market = {"current_price": 0.52, "id": "test", "question": "test"}
    dummy_signals = {"prices": [97000, 97050, 97100], "latest": 97100}
    try:
        result = bot.make_decision(dummy_market, dummy_signals)
        return result.get("action") in ("buy", "skip")
    except Exception as e:
        logger.error(f"  VALIDATION FAILED for {bot.name}: {e}")
        return False


def run_evolution(bots, cycle_number):
    """Run evolution cycle — kill bots below WR threshold, mutate from survivors."""
    logger.info(f"=== Evolution Cycle {cycle_number} ===")

    # Gather performance and classify by WR
    rankings = []
    for bot in bots:
        perf = bot.get_performance(hours=config.EVOLUTION_INTERVAL_HOURS)
        rankings.append({
            "name": bot.name,
            "strategy_type": bot.strategy_type,
            "generation": bot.generation,
            "pnl": perf["total_pnl"],
            "win_rate": perf["win_rate"],
            "trades": perf["total_trades"],
        })

    # Sort by WR (not P&L) for ranking display
    rankings.sort(key=lambda x: x["win_rate"], reverse=True)

    # Classify bots
    immune = []       # <MIN_TRADES resolved trades — not enough data
    above = []        # WR >= MIN_WIN_RATE with enough trades — survive
    below = []        # WR < MIN_WIN_RATE with enough trades — get replaced
    for r in rankings:
        if r["trades"] < config.MIN_TRADES_FOR_JUDGMENT:
            immune.append(r)
        elif r["win_rate"] >= config.MIN_WIN_RATE:
            above.append(r)
        else:
            below.append(r)

    logger.info("Rankings (WR-based):")
    for r in rankings:
        if r in immune:
            status = "IMMUNE"
        elif r in above:
            status = "SURVIVES"
        else:
            status = "REPLACED"
        logger.info(f"  {r['name']}: WR={r['win_rate']:.1%}, P&L=${r['pnl']:.2f}, Trades={r['trades']} [{status}]")

    # Safety net: if ALL would be killed (no immune + no above), keep best 1 by WR
    if not immune and not above and below:
        best = below.pop(0)  # rankings already sorted by WR desc, so first in below is best
        above.append(best)
        logger.info(f"  Safety net: keeping {best['name']} (best WR {best['win_rate']:.1%}) as sole survivor")

    # If nobody needs replacing, early return
    if not below:
        logger.info("  No bots below threshold — skipping evolution")
        for bot in bots:
            bot.reset_daily()
        return bots

    survivor_names = {r["name"] for r in immune + above}
    replaced_names = {r["name"] for r in below}

    new_bots = []
    for bot in bots:
        if bot.name in survivor_names:
            bot.reset_daily()
            new_bots.append(bot)

    # Create replacements from winners
    winners = [b for b in bots if b.name in survivor_names]
    replaced = [b for b in bots if b.name in replaced_names]

    for dead_bot in replaced:
        parent = random.choice(winners)
        evolved = create_evolved_bot(parent, dead_bot.strategy_type, cycle_number)

        # Inherit the dead bot's API key slot so evolved bot uses same Simmer account
        if hasattr(dead_bot, '_api_key_slot'):
            evolved._api_key_slot = dead_bot._api_key_slot
            logger.info(f"  {evolved.name} inherits slot {dead_bot._api_key_slot} from {dead_bot.name}")

        # Validate the new bot can actually trade before committing
        if not _validate_bot(evolved):
            logger.warning(f"  {evolved.name} failed validation, recreating with pure defaults")
            from bots.bot_momentum import DEFAULT_PARAMS as MOMENTUM_DEFAULTS
            from bots.bot_mean_rev import DEFAULT_PARAMS as MEANREV_DEFAULTS
            from bots.bot_hybrid import DEFAULT_PARAMS as HYBRID_DEFAULTS
            from bots.bot_sentiment import DEFAULT_PARAMS as SENTIMENT_DEFAULTS
            from bots.bot_sniper import DEFAULT_PARAMS as SNIPER_DEFAULTS
            fallback_map = {
                "momentum": MOMENTUM_DEFAULTS, "mean_reversion": MEANREV_DEFAULTS,
                "mean_reversion_sl": MEANREV_DEFAULTS, "mean_reversion_tp": MEANREV_DEFAULTS,
                "sniper": SNIPER_DEFAULTS,
                "sentiment": SENTIMENT_DEFAULTS, "hybrid": HYBRID_DEFAULTS,
            }
            bot_classes = {
                "momentum": MomentumBot, "mean_reversion": MeanRevBot,
                "mean_reversion_sl": MeanRevSLBot, "mean_reversion_tp": MeanRevTPBot,
                "sniper": SniperBot,
                "sentiment": SentimentBot, "hybrid": HybridBot,
            }
            cls = bot_classes.get(dead_bot.strategy_type, MomentumBot)
            fallback_params = fallback_map.get(dead_bot.strategy_type, MOMENTUM_DEFAULTS).copy()
            evolved = cls(
                name=evolved.name, params=fallback_params,
                generation=cycle_number, lineage=f"{parent.name} -> {evolved.name} (fallback)",
            )
            if hasattr(dead_bot, '_api_key_slot'):
                evolved._api_key_slot = dead_bot._api_key_slot

        db.retire_bot(dead_bot.name)
        db.save_bot_config(
            evolved.name, evolved.strategy_type, evolved.generation,
            evolved.strategy_params, evolved.lineage
        )

        new_bots.append(evolved)
        logger.info(f"  Created {evolved.name} (from {parent.name}): {json.dumps(evolved.strategy_params)[:200]}")

    # Log evolution event
    db.log_evolution(
        cycle_number,
        list(survivor_names),
        list(replaced_names),
        [b.name for b in new_bots if b.name not in survivor_names],
        rankings,
    )

    # Final validation: confirm all bots have API slots and can trade
    for bot in new_bots:
        slot = getattr(bot, '_api_key_slot', None)
        logger.info(f"  Post-evolution: {bot.name} ({bot.strategy_type}) slot={slot} params_keys={list(bot.strategy_params.keys())}")

    return new_bots


def load_api_key():
    try:
        with open(config.SIMMER_API_KEY_PATH) as f:
            return json.load(f).get("api_key")
    except FileNotFoundError:
        logger.error(f"No API key at {config.SIMMER_API_KEY_PATH}")
        return None


def discover_markets(api_key):
    """Find the active BTC 5-min up/down market."""
    import requests
    markets = []
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(
            f"{config.SIMMER_BASE_URL}/api/sdk/markets",
            headers=headers,
            params={"status": "active", "limit": 100},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            markets_list = data if isinstance(data, list) else data.get("markets", [])
            for m in markets_list:
                q = m.get("question", "").lower()
                has_btc = "btc" in q or "bitcoin" in q
                has_5min = any(kw in q for kw in config.TARGET_MARKET_KEYWORDS)
                if has_btc and has_5min:
                    markets.append(m)
    except Exception as e:
        logger.error(f"Market discovery error: {e}")
    logger.info(f"Discovered {len(markets)} BTC 5-min markets")
    return markets


def is_5min_market(question):
    """Check if this is a strict 5-minute window market (not 15-min or hourly)."""
    import re
    q = question.lower()
    # Match patterns like "10:00PM-10:05PM" (5-min range)
    range_match = re.search(r'(\d{1,2}):(\d{2})(am|pm)-(\d{1,2}):(\d{2})(am|pm)', q)
    if range_match:
        h1, m1 = int(range_match.group(1)), int(range_match.group(2))
        h2, m2 = int(range_match.group(4)), int(range_match.group(5))
        ap1, ap2 = range_match.group(3), range_match.group(6)
        # Convert to 24h
        if ap1 == 'pm' and h1 != 12: h1 += 12
        if ap1 == 'am' and h1 == 12: h1 = 0
        if ap2 == 'pm' and h2 != 12: h2 += 12
        if ap2 == 'am' and h2 == 12: h2 = 0
        diff = (h2 * 60 + m2) - (h1 * 60 + m1)
        if diff < 0: diff += 24 * 60
        return diff == 5
    return False


def expire_stale_trades():
    """Expire trades for 5-min markets that are >1h old and never resolved.
    These fell off Simmer's resolved API before we could check them."""
    with db.get_conn() as conn:
        count = conn.execute('''
            UPDATE trades SET outcome = 'expired', pnl = 0, resolved_at = datetime('now')
            WHERE outcome IS NULL AND created_at < datetime('now', '-1 hour')
        ''').rowcount
    if count > 0:
        logger.info(f"Expired {count} stale trades (>1h old, never resolved)")
    return count


def resolve_trades(api_key):
    """Check Simmer for resolved markets and update trade outcomes."""
    import requests
    try:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Get pending trades from our DB
        with db.get_conn() as conn:
            pending = conn.execute(
                "SELECT id, market_id, bot_name, side, amount, shares_bought, trade_features, reasoning FROM trades WHERE outcome IS NULL"
            ).fetchall()

        if not pending:
            return 0

        # Get unique market IDs we need to check
        market_ids = list({t["market_id"] for t in pending})

        # Fetch resolved markets from Simmer
        resp = requests.get(
            f"{config.SIMMER_BASE_URL}/api/sdk/markets",
            headers=headers,
            params={"status": "resolved", "limit": 200},
            timeout=15,
        )
        if resp.status_code != 200:
            return 0

        data = resp.json()
        markets_list = data if isinstance(data, list) else data.get("markets", [])

        # Build lookup: market_id -> market with outcome
        resolved_map = {}
        for m in markets_list:
            mid = m.get("id") or m.get("market_id")
            if mid in market_ids:
                resolved_map[mid] = m

        if not resolved_map:
            return 0

        count = 0
        for trade in pending:
            market_id = trade["market_id"]
            if market_id not in resolved_map:
                continue

            market = resolved_map[market_id]
            # outcome field: true = YES won, false = NO won
            market_outcome = market.get("outcome")
            if market_outcome is None:
                continue

            side = trade["side"]
            amount = trade["amount"]
            try:
                shares = trade["shares_bought"] or 0
            except (IndexError, KeyError):
                shares = 0

            # Did this bot's voted side win?
            if side == "yes":
                won = market_outcome is True
            else:
                won = market_outcome is False

            outcome = "win" if won else "loss"

            # P&L: win = shares pay $1 each minus cost; loss = lose entire cost
            if shares > 0:
                pnl = (shares - amount) if won else -amount
            else:
                pnl = 0  # This bot voted but wasn't the executor

            db.resolve_trade(trade["id"], outcome, pnl)

            # Learn from outcome using features captured AT TRADE TIME (not resolution time)
            try:
                stored_features = trade["trade_features"]
                if stored_features:
                    features = json.loads(stored_features)
                else:
                    # Fallback: extract features from reasoning text
                    try:
                        reasoning = trade["reasoning"]
                    except (KeyError, IndexError):
                        reasoning = None
                    features = learning.extract_features_from_reasoning(reasoning)
            except (KeyError, json.JSONDecodeError):
                features = None

            if features:
                learning.record_outcome(trade["bot_name"], features, side, won)

            count += 1

        if count > 0:
            logger.info(f"Resolved {count} trades ({sum(1 for t in pending if resolved_map.get(t['market_id']))} pending matched {len(resolved_map)} resolved markets)")
        return count

    except Exception as e:
        logger.error(f"Trade resolution error: {e}")
        return 0


def load_bot_keys():
    """Load per-bot API keys. Returns dict of bot_name -> api_key."""
    try:
        with open(config.SIMMER_BOT_KEYS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def assign_bot_slots(bots, bot_keys, default_key):
    """Assign each bot to a Simmer account slot.

    Slots are named: slot_0, slot_1, slot_2, slot_3
    Each slot maps to a Simmer API key. When a bot is replaced during
    evolution, the new bot inherits the dead bot's slot (and API key).
    Bots that already have a slot (from evolution inheritance) keep it.
    """
    all_slots = ["slot_0", "slot_1", "slot_2", "slot_3"]

    # First pass: collect already-assigned slots
    used_slots = set()
    for bot in bots:
        if hasattr(bot, '_api_key_slot') and bot._api_key_slot:
            used_slots.add(bot._api_key_slot)

    # Second pass: assign free slots to bots that don't have one
    free_slots = [s for s in all_slots if s not in used_slots]
    for bot in bots:
        if not hasattr(bot, '_api_key_slot') or not bot._api_key_slot:
            if free_slots:
                bot._api_key_slot = free_slots.pop(0)
            else:
                bot._api_key_slot = all_slots[0]  # fallback

    for bot in bots:
        key = bot_keys.get(bot._api_key_slot, default_key)
        logger.info(f"  {bot.name} -> {bot._api_key_slot} (key: ...{key[-8:]})")


class PositionMonitorThread(threading.Thread):
    """Background thread that polls Simmer for market prices every 0.5s.

    Monitors all open positions belonging to bots with exit strategies
    (stop_loss, take_profit). When a position hits its SL/TP threshold,
    closes it immediately in the DB and logs the exit.

    The thread fetches active market prices from Simmer in a single API call
    per tick, then checks all open positions against those prices.
    """

    def __init__(self, api_key):
        super().__init__(daemon=True, name="position-monitor")
        self.api_key = api_key
        self._bots = {}  # name -> bot instance
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def update_bots(self, bots):
        """Update the bot roster (called from main thread after evolution)."""
        with self._lock:
            self._bots = {b.name: b for b in bots if b.exit_strategy}

    def stop(self):
        self._stop_event.set()

    def _fetch_market_prices(self):
        """Fetch current prices for all active markets from Simmer."""
        import requests
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = requests.get(
                f"{config.SIMMER_BASE_URL}/api/sdk/markets",
                headers=headers,
                params={"status": "active", "limit": 100},
                timeout=5,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            markets_list = data if isinstance(data, list) else data.get("markets", [])
            return {
                (m.get("id") or m.get("market_id")): m.get("current_price")
                for m in markets_list
                if m.get("current_price") is not None
            }
        except Exception:
            return {}

    def _check_positions(self, price_map):
        """Check all open positions for SL/TP exits."""
        with self._lock:
            exit_bots = dict(self._bots)

        if not exit_bots:
            return

        # Get open trades for exit-strategy bots
        bot_names = list(exit_bots.keys())
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, bot_name, market_id, side, amount, shares_bought, trade_features, reasoning "
                "FROM trades WHERE outcome IS NULL AND bot_name IN ({})".format(
                    ",".join("?" for _ in bot_names)
                ),
                bot_names,
            ).fetchall()

        if not rows:
            return

        for trade in rows:
            market_id = trade["market_id"]
            current_yes_price = price_map.get(market_id)
            if current_yes_price is None:
                continue

            bot = exit_bots.get(trade["bot_name"])
            if not bot:
                continue

            side = trade["side"]
            amount = trade["amount"]
            try:
                shares = trade["shares_bought"] or 0
            except (KeyError, IndexError):
                shares = 0
            if shares <= 0:
                continue

            entry_price = amount / shares

            if side == "yes":
                current_share_price = current_yes_price
            else:
                current_share_price = 1.0 - current_yes_price

            if entry_price <= 0:
                continue
            pnl_pct = (current_share_price - entry_price) / entry_price

            exit_reason = None
            exit_pnl = None

            if bot.exit_strategy == "stop_loss" and pnl_pct <= -bot.stop_loss_pct:
                exit_pnl = (current_share_price - entry_price) * shares
                exit_reason = f"exit_sl ({pnl_pct:+.1%})"

            if bot.exit_strategy == "take_profit" and pnl_pct >= bot.take_profit_pct:
                exit_pnl = (current_share_price - entry_price) * shares
                exit_reason = f"exit_tp ({pnl_pct:+.1%})"

            if exit_reason and exit_pnl is not None:
                outcome = "exit_tp" if "tp" in exit_reason else "exit_sl"
                db.resolve_trade(trade["id"], outcome, exit_pnl)
                logger.info(
                    f"[{trade['bot_name']}] EARLY EXIT: {exit_reason} on {market_id[:12]}... "
                    f"entry=${entry_price:.3f} now=${current_share_price:.3f} pnl=${exit_pnl:+.2f}"
                )

                # Feed into learning
                try:
                    stored = trade["trade_features"]
                    if stored:
                        features = json.loads(stored)
                    else:
                        try:
                            features = learning.extract_features_from_reasoning(trade["reasoning"])
                        except (KeyError, IndexError):
                            features = None
                except (KeyError, json.JSONDecodeError):
                    features = None

                if features:
                    won = exit_pnl > 0
                    learning.record_outcome(trade["bot_name"], features, side, won)

    def run(self):
        """Main monitor loop — polls every 0.5s."""
        logger.info(f"Position monitor started (polling every {FAST_POLL_INTERVAL}s)")
        consecutive_errors = 0

        while not self._stop_event.is_set():
            try:
                # Only fetch prices if there are bots to monitor
                with self._lock:
                    has_bots = bool(self._bots)

                if has_bots:
                    price_map = self._fetch_market_prices()
                    if price_map:
                        self._check_positions(price_map)
                        consecutive_errors = 0
                    else:
                        consecutive_errors += 1

                # Back off on repeated API failures to avoid hammering Simmer
                if consecutive_errors > 10:
                    self._stop_event.wait(5)
                elif consecutive_errors > 3:
                    self._stop_event.wait(2)
                else:
                    self._stop_event.wait(FAST_POLL_INTERVAL)

            except Exception as e:
                logger.error(f"Position monitor error: {e}")
                consecutive_errors += 1
                self._stop_event.wait(2)


def main_loop(bots, api_key):
    """Main trading loop — each bot trades independently on its own Simmer account."""
    price_feed = get_price_feed()
    sentiment_feed = get_sentiment_feed()
    orderflow_feed = get_orderflow_feed()

    price_feed.start()
    sentiment_feed.start()
    orderflow_feed.start()

    evolution_interval = config.EVOLUTION_INTERVAL_HOURS * 3600

    # Restore evolution state from DB so it survives restarts
    saved_cycle = db.get_arena_state("evolution_cycle", "0")
    cycle_number = int(saved_cycle)
    saved_last_evo = db.get_arena_state("last_evolution_time")
    if saved_last_evo:
        last_evolution = float(saved_last_evo)
        elapsed = time.time() - last_evolution
        logger.info(f"Restored evolution timer: cycle {cycle_number}, {elapsed/3600:.1f}h since last evolution")
    else:
        last_evolution = time.time()
        # Persist the initial start so it survives restarts before first evolution
        db.set_arena_state("last_evolution_time", str(last_evolution))
        db.set_arena_state("evolution_cycle", "0")
        logger.info("No saved evolution state, starting fresh timer (persisted)")

    # Throttle resolve/expire — only run every RESOLVE_INTERVAL
    last_resolve_time = 0  # Run immediately on first iteration

    # Load recently traded (bot_name, market_id) pairs from DB to prevent
    # duplicate trades across restarts
    traded = set()
    with db.get_conn() as conn:
        recent = conn.execute(
            "SELECT bot_name, market_id FROM trades WHERE created_at >= datetime('now', '-6 hours')"
        ).fetchall()
        for r in recent:
            traded.add((r["bot_name"], r["market_id"]))
    logger.info(f"Loaded {len(traded)} recent trade keys from DB (dedup across restarts)")

    # Load per-bot API keys and assign slots
    bot_keys = load_bot_keys()
    assign_bot_slots(bots, bot_keys, api_key)
    multi_account = len(bot_keys) >= config.NUM_BOTS
    if multi_account:
        logger.info(f"Multi-account mode: {len(bot_keys)} Simmer accounts loaded")
    else:
        logger.info(f"Single-account mode: {len(bot_keys)} bot keys found (need {config.NUM_BOTS} for independent trading)")

    logger.info(f"Arena started with {len(bots)} bots in {config.get_current_mode()} mode")
    logger.info(f"Bots: {[b.name for b in bots]}")
    logger.info(f"Evolution every {config.EVOLUTION_INTERVAL_HOURS}h")

    # Start fast position monitor thread (polls Simmer every 0.5s for SL/TP)
    pos_monitor = PositionMonitorThread(api_key)
    pos_monitor.update_bots(bots)
    pos_monitor.start()

    while True:
        try:
            # Check for evolution
            if time.time() - last_evolution >= evolution_interval:
                cycle_number += 1
                bots = run_evolution(bots, cycle_number)
                last_evolution = time.time()
                # Persist evolution state so it survives restarts
                db.set_arena_state("evolution_cycle", str(cycle_number))
                db.set_arena_state("last_evolution_time", str(last_evolution))
                traded.clear()
                # Re-assign slots — new bots inherit the killed bot's slot index
                assign_bot_slots(bots, bot_keys, api_key)
                # Update position monitor with new bot roster
                pos_monitor.update_bots(bots)

            # Resolve completed trades + expire stale (throttled to every 60s)
            now = time.time()
            if now - last_resolve_time >= RESOLVE_INTERVAL:
                if multi_account:
                    for slot_key in set(bot_keys.values()):
                        resolve_trades(slot_key)
                else:
                    resolve_trades(api_key)
                expire_stale_trades()
                last_resolve_time = now

            # Discover active markets (any key works for read-only)
            markets = discover_markets(api_key)
            if not markets:
                logger.debug("No active 5-min markets found, waiting...")
                # Position monitor thread handles SL/TP independently
                time.sleep(30)
                continue

            # Filter to strict 5-minute window markets only
            five_min_markets = [m for m in markets if is_5min_market(m.get("question", ""))]
            if not five_min_markets:
                logger.debug(f"Found {len(markets)} BTC markets but none are strict 5-min windows, waiting...")
                time.sleep(30)
                continue

            # Compute window age and filter out late-window markets
            now_utc = datetime.now(timezone.utc)
            tradeable_markets = []
            for m in five_min_markets:
                resolves_at_str = m.get("resolves_at") or m.get("end_time")
                if resolves_at_str:
                    try:
                        # Parse ISO format: "2026-02-17 03:10:00Z" or "2026-02-17T03:10:00Z"
                        rat = resolves_at_str.replace("Z", "+00:00").replace(" ", "T")
                        resolves_at = datetime.fromisoformat(rat)
                        time_remaining = (resolves_at - now_utc).total_seconds()
                        window_age = 300 - time_remaining  # 5-min windows = 300s

                        m["time_remaining_seconds"] = time_remaining
                        m["window_age_seconds"] = window_age

                        if time_remaining < 0:
                            logger.debug(f"Skipping expired market (remaining={time_remaining:.0f}s): {m.get('question', '')[:50]}")
                            continue
                        if time_remaining < 90:
                            logger.debug(f"Skipping late-window market (remaining={time_remaining:.0f}s): {m.get('question', '')[:50]}")
                            continue
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Could not parse resolves_at '{resolves_at_str}': {e}")
                        # Still tradeable, just without time context
                        m["time_remaining_seconds"] = None
                        m["window_age_seconds"] = None

                tradeable_markets.append(m)

            if not tradeable_markets:
                logger.debug(f"All {len(five_min_markets)} 5-min markets filtered out (late-window), waiting...")
                time.sleep(TRADE_INTERVAL)
                continue

            five_min_markets = tradeable_markets

            # Gather signals
            price_signals = price_feed.get_signals("btc")
            sent_signals = sentiment_feed.get_signals("btc")

            new_trades = 0
            for market in five_min_markets:
                market_id = market.get("id") or market.get("market_id")
                of_signals = orderflow_feed.get_signals(market_id, api_key)
                combined_signals = {**price_signals, **sent_signals, **of_signals}

                # Each bot trades independently on its own account
                for bot in bots:
                    key = (bot.name, market_id)
                    if key in traded:
                        continue

                    try:
                        signal = bot.make_decision(market, combined_signals)

                        # Skip if bot sees no edge
                        if signal.get("action") == "skip":
                            traded.add(key)
                            continue

                        result = bot.execute(signal, market)
                        traded.add(key)
                        if result.get("success"):
                            new_trades += 1
                            logger.info(f"[{bot.name}] {signal['side'].upper()} ${signal['suggested_amount']:.2f} (conf={signal['confidence']:.2f}) on {market.get('question', '')[:50]}")
                        else:
                            logger.debug(f"[{bot.name}] Trade failed on {market_id}: {result.get('reason')}")
                    except Exception as e:
                        logger.error(f"[{bot.name}] Error on {market_id}: {e}")
                        traded.add(key)

            if new_trades > 0:
                logger.info(f"Placed {new_trades} new trades this cycle")

            # Position monitor thread polls Simmer every 0.5s for SL/TP
            time.sleep(TRADE_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Arena stopped by user")
            break
        except Exception as e:
            logger.error(f"Arena loop error: {e}")
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Polymarket Bot Arena")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Trading mode (default: from config)")
    parser.add_argument("--setup", action="store_true", help="Run setup verification first")
    args = parser.parse_args()

    if args.mode:
        if args.mode == "live":
            confirm = input("You are switching to LIVE trading with real USDC. Type YES to confirm: ")
            if confirm.strip() != "YES":
                print("Cancelled. Staying in paper mode.")
                sys.exit(0)
        config.set_trading_mode(args.mode)
        logger.info(f"Trading mode set to: {args.mode}")

    if args.setup:
        import setup
        if not setup.main():
            sys.exit(1)

    api_key = load_api_key()
    if not api_key:
        print("No Simmer API key found. Run: python3 setup.py")
        sys.exit(1)

    bots = create_default_bots()

    # Save initial bot configs (only if not already saved)
    existing = {b["bot_name"] for b in db.get_active_bots()}
    for bot in bots:
        if bot.name not in existing:
            db.save_bot_config(bot.name, bot.strategy_type, bot.generation, bot.strategy_params)

    # Load per-bot trading modes from DB
    for bot in bots:
        bot.trading_mode = db.get_bot_mode(bot.name)

    # Backfill learning data from old resolved trades that had no trade_features
    backfilled = learning.backfill_from_resolved_trades()
    if backfilled:
        logger.info(f"Backfilled learning from {backfilled} historical trades")

    main_loop(bots, api_key)


if __name__ == "__main__":
    main()
