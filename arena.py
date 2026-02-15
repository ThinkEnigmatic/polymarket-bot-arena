"""Bot Arena Manager — runs 4 competing bots with 12-hour evolution cycles."""

import argparse
import json
import logging
import sys
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

import config
import db
import learning
from bots.bot_momentum import MomentumBot
from bots.bot_mean_rev import MeanRevBot
from bots.bot_sentiment import SentimentBot
from bots.bot_hybrid import HybridBot
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
TRADE_INTERVAL = 60  # Check markets every 60s


def create_default_bots():
    """Create the 4 starting bots."""
    return [
        MomentumBot(name="momentum-v1", generation=0),
        MeanRevBot(name="meanrev-v1", generation=0),
        SentimentBot(name="sentiment-v1", generation=0),
        HybridBot(name="hybrid-v1", generation=0),
    ]


def create_evolved_bot(winner, loser_type, gen_number):
    """Create an evolved bot based on the winner's params."""
    winner_export = winner.export_params()
    new_params = winner.mutate(winner_export["params"])
    name = f"{loser_type}-g{gen_number}-{random.randint(100,999)}"

    bot_classes = {
        "momentum": MomentumBot,
        "mean_reversion": MeanRevBot,
        "sentiment": SentimentBot,
        "hybrid": HybridBot,
    }
    cls = bot_classes.get(loser_type, MomentumBot)
    return cls(
        name=name,
        params=new_params,
        generation=gen_number,
        lineage=f"{winner.name} -> {name}",
    )


def run_evolution(bots, cycle_number):
    """Run the 12-hour evolution cycle."""
    logger.info(f"=== Evolution Cycle {cycle_number} ===")

    # Rank bots by P&L over the evolution window
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

    rankings.sort(key=lambda x: x["pnl"], reverse=True)
    logger.info("Rankings:")
    for i, r in enumerate(rankings):
        status = "SURVIVES" if i < config.SURVIVORS_PER_CYCLE else "REPLACED"
        logger.info(f"  #{i+1} {r['name']}: P&L=${r['pnl']:.2f}, WR={r['win_rate']:.1%}, Trades={r['trades']} [{status}]")

    # Survivors
    survivors = bots[:2] if rankings[0]["name"] == bots[0].name else []
    # Actually match by name
    survivor_names = {rankings[i]["name"] for i in range(config.SURVIVORS_PER_CYCLE)}
    replaced_names = {rankings[i]["name"] for i in range(config.SURVIVORS_PER_CYCLE, len(rankings))}

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
        db.retire_bot(dead_bot.name)
        db.save_bot_config(
            evolved.name, evolved.strategy_type, evolved.generation,
            evolved.strategy_params, evolved.lineage
        )
        # Save evolved params to file
        evolved_dir = Path(__file__).parent / "bots" / "evolved"
        evolved_dir.mkdir(exist_ok=True)
        with open(evolved_dir / f"{evolved.name}.json", "w") as f:
            json.dump(evolved.export_params(), f, indent=2)

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
                "SELECT id, market_id, bot_name, side, amount, shares_bought, trade_features FROM trades WHERE outcome IS NULL"
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
                    # Fallback for old trades without stored features — skip learning
                    features = None
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
    evolution, the new bot inherits the same slot (and API key).
    """
    slot_names = ["slot_0", "slot_1", "slot_2", "slot_3"]
    for i, bot in enumerate(bots):
        slot = slot_names[i] if i < len(slot_names) else slot_names[0]
        bot._api_key_slot = slot
        key = bot_keys.get(slot, default_key)
        logger.info(f"  {bot.name} -> {slot} (key: ...{key[-8:]})")


def main_loop(bots, api_key):
    """Main trading loop — each bot trades independently on its own Simmer account."""
    price_feed = get_price_feed()
    sentiment_feed = get_sentiment_feed()
    orderflow_feed = get_orderflow_feed()

    price_feed.start()
    sentiment_feed.start()
    orderflow_feed.start()

    cycle_number = 0
    last_evolution = time.time()
    evolution_interval = config.EVOLUTION_INTERVAL_HOURS * 3600

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

    while True:
        try:
            # Check for evolution
            if time.time() - last_evolution >= evolution_interval:
                cycle_number += 1
                bots = run_evolution(bots, cycle_number)
                last_evolution = time.time()
                traded.clear()
                # Re-assign slots — new bots inherit the killed bot's slot index
                assign_bot_slots(bots, bot_keys, api_key)

            # Resolve completed trades (check all accounts)
            if multi_account:
                for slot_key in set(bot_keys.values()):
                    resolve_trades(slot_key)
            else:
                resolve_trades(api_key)

            # Clean up stale trades that fell off the resolved API
            expire_stale_trades()

            # Discover active markets (any key works for read-only)
            markets = discover_markets(api_key)
            if not markets:
                logger.debug("No active 5-min markets found, waiting...")
                time.sleep(30)
                continue

            # Filter to strict 5-minute window markets only
            five_min_markets = [m for m in markets if is_5min_market(m.get("question", ""))]
            if not five_min_markets:
                logger.debug(f"Found {len(markets)} BTC markets but none are strict 5-min windows, waiting...")
                time.sleep(30)
                continue

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

    main_loop(bots, api_key)


if __name__ == "__main__":
    main()
