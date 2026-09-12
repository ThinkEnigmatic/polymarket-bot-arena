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
from bots.bot_phantom import PhantomBot
from bots.bot_btc_maker import BtcMakerBot
from bots.bot_late_window_maker import LateWindowMakerBot
from bots.bot_fee_zone_maker import FeeZoneMakerBot
from bots.bot_copy import CopyBot
from signals.price_feed import get_feed as get_price_feed
from signals.sentiment import get_feed as get_sentiment_feed
from signals.orderflow import get_feed as get_orderflow_feed
from signals.polymarket_prices import get_feed as get_pm_price_feed
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
maker_logger = logging.getLogger("arena.maker")

# Market check interval (seconds)
TRADE_INTERVAL = 15    # Discover markets + place trades every 15s (fast market discovery)
RESOLVE_INTERVAL = 60  # Resolve trades + expire stale every 60s (expensive, no need to rush)
FAST_POLL_INTERVAL = 0.5  # Poll market prices for SL/TP exits every 0.5s
MARKET_WINDOW_SECONDS = 300
MIN_ENTRY_SECONDS_REMAINING = 60
MARKET_CLOCK_SKEW_GRACE_SECONDS = 5


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
            "phantom": PhantomBot,
            "sentiment": SentimentBot,
            "hybrid": HybridBot,
        }
        # Maker and copy bots are managed separately — exclude from taker list.
        MAKER_TYPES = {"late_window_maker", "fee_zone_maker", "btc_maker", "copy_trade"}
        bots = []
        for cfg in active:
            if cfg["strategy_type"] in MAKER_TYPES:
                continue
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
        SniperBot(name="sniper-v1", generation=0),
        PhantomBot(name="phantom-v1", generation=0),
    ]


def create_evolved_bot(winner, gen_number):
    """Create a mutated child of the profitable strategy that won.

    Evolution previously recreated the losing strategy type, which meant a
    winning family could never reproduce. A child now inherits both the
    winner's class and its full parameter schema before mutation.
    """
    winner_params = winner.export_params()["params"]
    new_params = winner.mutate(winner_params)
    name = f"{winner.strategy_type}-g{gen_number}-{random.randint(100,999)}"

    return type(winner)(
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
        return result.get("action") in ("buy", "skip", "hold")
    except Exception as e:
        logger.error(f"  VALIDATION FAILED for {bot.name}: {e}")
        return False


def run_evolution(bots, cycle_number):
    """Replace sufficiently tested money-losers with profitable offspring."""
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
            "avg_pnl": perf.get("avg_pnl", 0),
            "win_rate": perf["win_rate"],
            "trades": perf["total_trades"],
        })

    # Entry price changes the breakeven win rate, so dollars earned—not raw
    # win rate—is the selection objective.
    rankings.sort(
        key=lambda x: (x["pnl"], x["avg_pnl"], x["win_rate"]), reverse=True
    )

    # Classify bots
    immune = []       # <MIN_TRADES resolved trades — not enough data
    above = []        # Positive P&L with enough trades — survive and reproduce
    below = []        # Non-positive P&L with enough trades — get replaced
    for r in rankings:
        if r["trades"] < config.MIN_TRADES_FOR_JUDGMENT:
            immune.append(r)
        elif r["pnl"] > 0:
            above.append(r)
        else:
            below.append(r)

    logger.info("Rankings (P&L-based):")
    for r in rankings:
        if r in immune:
            status = "IMMUNE"
        elif r in above:
            status = "SURVIVES"
        else:
            status = "REPLACED"
        logger.info(f"  {r['name']}: WR={r['win_rate']:.1%}, P&L=${r['pnl']:.2f}, Trades={r['trades']} [{status}]")

    # Safety net: if all tested bots lost money, keep the least-bad one as the
    # parent rather than attempting to reproduce from an untested bot.
    if not above and below:
        best = below.pop(0)
        above.append(best)
        logger.info(
            f"  Safety net: keeping {best['name']} "
            f"(best P&L ${best['pnl']:.2f}) as sole parent"
        )

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
    winner_names = {r["name"] for r in above}
    winners = [b for b in bots if b.name in winner_names]
    replaced = [b for b in bots if b.name in replaced_names]

    for dead_bot in replaced:
        parent = random.choice(winners)
        evolved = create_evolved_bot(parent, cycle_number)

        # Inherit the dead bot's API key slot so evolved bot uses same Simmer account
        if hasattr(dead_bot, '_api_key_slot'):
            evolved._api_key_slot = dead_bot._api_key_slot
            logger.info(f"  {evolved.name} inherits slot {dead_bot._api_key_slot} from {dead_bot.name}")

        # Validate the new bot can actually trade before committing
        if not _validate_bot(evolved):
            logger.warning(
                f"  {evolved.name} failed validation, recreating as an "
                "unmutated copy of its parent"
            )
            evolved = type(parent)(
                name=evolved.name,
                params=parent.export_params()["params"],
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
                if has_btc and is_5min_market(q):
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
    range_match = re.search(
        r'(\d{1,2}):(\d{2})\s*(am|pm)\s*[-–]\s*'
        r'(\d{1,2}):(\d{2})\s*(am|pm)',
        q,
    )
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
    return any(marker in q for marker in ("5 min", "5-min", "5min", "5 minute"))


def filter_tradeable_markets(markets, now_utc=None):
    """Keep only markets whose actual five-minute window is active.

    Signals observed now are not predictive inputs for a market whose window
    starts later. Missing or unparseable resolution times therefore fail closed.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    tradeable = []
    too_late = 0
    not_started = 0
    invalid_time = 0

    for market in markets:
        resolves_at_str = market.get("resolves_at") or market.get("end_time")
        if not resolves_at_str:
            invalid_time += 1
            continue
        try:
            normalized = resolves_at_str.replace("Z", "+00:00").replace(" ", "T")
            resolves_at = datetime.fromisoformat(normalized)
            if resolves_at.tzinfo is None:
                resolves_at = resolves_at.replace(tzinfo=timezone.utc)
            time_remaining = (resolves_at - now_utc).total_seconds()
        except (AttributeError, ValueError, TypeError):
            invalid_time += 1
            continue

        market["time_remaining_seconds"] = time_remaining
        market["window_age_seconds"] = max(0, MARKET_WINDOW_SECONDS - time_remaining)

        if time_remaining < MIN_ENTRY_SECONDS_REMAINING:
            too_late += 1
            continue
        if time_remaining > MARKET_WINDOW_SECONDS + MARKET_CLOCK_SKEW_GRACE_SECONDS:
            not_started += 1
            continue
        tradeable.append(market)

    return tradeable, too_late, not_started, invalid_time


def expire_stale_trades():
    """Expire trades for 5-min markets that are >1h old and never resolved.
    These fell off Simmer's resolved API before we could check them."""
    with db.get_conn() as conn:
        count = conn.execute('''
            UPDATE trades SET outcome = 'expired', pnl = 0, resolved_at = datetime('now')
            WHERE outcome IS NULL AND mode = 'paper'
              AND created_at < datetime('now', '-1 hour')
        ''').rowcount
    if count > 0:
        logger.info(f"Expired {count} stale trades (>1h old, never resolved)")
    return count


def run_maker_section(maker_bot, market, signals, traded):
    """Run one BtcMakerBot paper-trading cycle on a single market.

    Always paper-only: the bot's trading_mode is forced to 'paper' before every
    call so that even if the DB row were toggled to 'live' by mistake, no real
    Polymarket orders are ever placed from here.

    Logs maker metrics (edge, bid, ask) on every cycle regardless of whether a
    trade is placed.

    Returns True if a paper trade was recorded, False otherwise.
    """
    # --- Safety: enforce paper mode unconditionally ---
    maker_bot.trading_mode = "paper"

    market_id = market.get("id") or market.get("market_id")
    key = (maker_bot.name, market_id)
    if key in traded:
        return False

    try:
        # Use analyze() directly to get full maker signal (bid/ask/edge).
        # We bypass make_decision() because the base class strips maker fields
        # and applies arena-wide guards (NO-bet ban, high-price guard) that are
        # not appropriate for a market-maker strategy.
        signal = maker_bot.analyze(market, signals)

        market_price = market.get("current_price", 0.5)
        maker_bid = signal.get("maker_bid")
        maker_ask = signal.get("maker_ask")
        maker_mid = signal.get("maker_mid")
        maker_side = signal.get("maker_side", "both")
        edge_bps = abs((maker_mid or market_price) - market_price) * 10000 if maker_mid is not None else 0.0

        # Always log maker metrics so we can track quoting behaviour over time
        maker_logger.info(
            f"[{maker_bot.name}] market={market_id[:12]}... "
            f"price={market_price:.3f} "
            f"bid={maker_bid:.3f} ask={maker_ask:.3f} mid={maker_mid:.3f} "
            f"edge={edge_bps:.1f}bps lean={maker_side} "
            f"conf={signal.get('confidence', 0.0):.3f}"
        )

        if signal.get("action") == "hold":
            # Edge too thin — skip, but still mark as visited this cycle
            traded.add(key)
            maker_logger.debug(
                f"[{maker_bot.name}] HOLD (edge too thin): {signal.get('reasoning', '')}"
            )
            return False

        # Execute the experiment directly on Simmer. Calling execute() would
        # re-read a dashboard mode toggle and could accidentally route this
        # explicitly paper-only experiment to the live CLOB.
        max_pos = config.get_max_position("paper")
        amount = min(signal.get("suggested_amount", max_pos * 0.5), max_pos)
        result = maker_bot._execute_paper(
            signal, market, amount, "simmer", "paper"
        )
        traded.add(key)

        if result.get("success"):
            maker_logger.info(
                f"[{maker_bot.name}] PAPER {signal['side'].upper()} "
                f"${signal.get('suggested_amount', 0):.2f} "
                f"bid={maker_bid:.3f} ask={maker_ask:.3f} edge={edge_bps:.1f}bps "
                f"on {market.get('question', '')[:50]}"
            )
            return True
        else:
            maker_logger.debug(
                f"[{maker_bot.name}] paper execute skipped: {result.get('reason')}"
            )
            return False

    except Exception as e:
        maker_logger.error(f"[{maker_bot.name}] Maker section error on {market_id}: {e}")
        traded.add(key)
        return False


def _create_copy_bots() -> list:
    """Instantiate copy-trade bots from DB wallet list.

    Wallets are stored in copytrading_wallets.  Add a wallet via:
        db.add_copy_wallet("0xABC...", label="tracked-wallet", mode="paper")
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT address, label, trading_mode FROM copytrading_wallets WHERE active=1"
        ).fetchall()

    bots = []
    for r in rows:
        mode = "paper"
        try:
            mode = r["trading_mode"] or "paper"
        except (IndexError, KeyError):
            pass
        bot = CopyBot(
            wallet_address=r["address"],
            label=r["label"] or r["address"][:16],
            mode=mode,
            max_size=5.0,
            size_fraction=0.10,
        )
        bots.append(bot)
        logger.info(f"Copy bot: [{bot.label}] wallet={r['address'][:16]}... mode={mode}")
    return bots


def _start_wallet_monitors(copy_bots: list):
    """Attach a real-time WalletMonitor to each copy bot and start it.

    Each monitor subscribes to Polygon newHeads (~2s block time) and
    immediately polls Polymarket activity on every block.  This reduces
    copy-trade latency from ~30s (polling) to ~2-5s (event-driven).
    Falls back to 15s polling if the WebSocket is unavailable.
    """
    try:
        from signals.wallet_monitor import WalletMonitor
    except ImportError as e:
        logger.warning(f"WalletMonitor unavailable ({e}) — using polling fallback")
        return

    for bot in copy_bots:
        monitor = WalletMonitor(bot.wallet, label=bot.label)
        bot.attach_monitor(monitor)
        monitor.start()


def _create_maker_bots():
    """Instantiate the fixed experimental maker bots.

    These run in parallel with the evolving taker bots but are NOT part of
    evolution — they persist across cycles so we can compare their long-term
    performance against each other and against the takers.

    Two competing hypotheses:
      late-window-maker-v1  — time-gated (final 90s), momentum-confirmed, large bets
      fee-zone-maker-v1     — always-on, fee-zone-aware, smaller bets, higher frequency
    """
    maker_bots = [
        LateWindowMakerBot(name="late-window-maker-v1"),
        FeeZoneMakerBot(name="fee-zone-maker-v1"),
    ]
    # Persist configs so the dashboard and DB queries can find them
    existing = {b["bot_name"] for b in db.get_active_bots()}
    for bot in maker_bots:
        if bot.name not in existing:
            db.save_bot_config(
                bot.name, bot.strategy_type, bot.generation, bot.strategy_params
            )
            logger.info(f"Registered maker bot: {bot.name} ({bot.strategy_type})")
    return maker_bots


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
    """Dormant monitor retained until venue-backed sell execution is added.

    Updating only the database does not close a Simmer or Polymarket position.
    Synthetic SL/TP accounting is therefore disabled to keep recorded P&L and
    actual exposure consistent.
    """

    def __init__(self, api_key):
        super().__init__(daemon=True, name="position-monitor")
        self.api_key = api_key
        self._bots = {}  # name -> bot instance
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def update_bots(self, bots):
        """Reject synthetic exit strategies until actual sell orders exist."""
        requested = [b.name for b in bots if b.exit_strategy]
        with self._lock:
            self._bots = {}
        if requested:
            logger.warning(
                "SL/TP monitor disabled for %s: no venue-backed exit order "
                "implementation; positions will hold to resolution",
                requested,
            )

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
    pm_price_feed = get_pm_price_feed()

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
    # duplicate trades across restarts. 1h lookback is enough for 5-min markets.
    traded = set()
    with db.get_conn() as conn:
        recent = conn.execute(
            "SELECT bot_name, market_id FROM trades WHERE created_at >= datetime('now', '-1 hours')"
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

    # Create fixed maker bots (paper-only, not part of evolution)
    maker_bots = _create_maker_bots()
    logger.info(f"Maker bots (experimental, paper-only): {[b.name for b in maker_bots]}")

    # Create copy bots (follow tracked wallets) + attach real-time monitors
    copy_bots = _create_copy_bots()
    if copy_bots:
        logger.info(f"Copy bots: {[b.label for b in copy_bots]}")
        _start_wallet_monitors(copy_bots)

    logger.info(f"Arena started with {len(bots)} bots in {config.get_current_mode()} mode")
    logger.info(f"Bots: {[b.name for b in bots]}")
    logger.info(f"Evolution every {config.EVOLUTION_INTERVAL_HOURS}h")

    # The monitor remains dormant until venue-backed exit orders are implemented.
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
                # Keep synthetic exit accounting disabled after evolution.
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

            # --- Copy section (runs every cycle regardless of BTC market availability) ---
            # Build token→market index from whatever markets Simmer has right now.
            # Even when there are no tradeable BTC windows, copy bots should still
            # drain their monitor queues and process any freshly detected whale trades.
            if copy_bots:
                copy_markets_by_token: dict = {}
                for _m in (markets or []):
                    _yt = _m.get("polymarket_token_id")
                    _nt = _m.get("polymarket_no_token_id")
                    if _yt:
                        copy_markets_by_token[_yt] = _m
                    if _nt:
                        copy_markets_by_token[_nt] = _m
                for copy_bot in copy_bots:
                    try:
                        n = copy_bot.check_and_copy(copy_markets_by_token, api_key)
                        if n > 0:
                            logger.info(
                                f"Copy bot [{copy_bot.label}]: mirrored {n} trades this cycle"
                            )
                    except Exception as e:
                        logger.error(f"Copy bot [{copy_bot.label}] error: {e}")

            if not markets:
                logger.debug("No active 5-min markets found, waiting...")
                time.sleep(30)
                continue

            tradeable_markets, past_markets, future_markets, invalid_markets = (
                filter_tradeable_markets(markets)
            )

            logger.info(
                f"Market filter: {len(markets)} strict BTC 5-min, "
                f"{past_markets} expired/too-late, {future_markets} not-started, "
                f"{invalid_markets} invalid-time, "
                f"{len(tradeable_markets)} eligible"
            )

            if not tradeable_markets:
                logger.debug("No eligible markets found, waiting...")
                time.sleep(TRADE_INTERVAL)
                continue

            # Trade ALL eligible markets, sorted soonest-first
            tradeable_markets.sort(key=lambda x: x.get("time_remaining_seconds", 999999))
            selected_market = tradeable_markets[0]  # used for maker section (one market at a time)

            for m in tradeable_markets:
                mid = m.get("id") or m.get("market_id")
                tr = m.get("time_remaining_seconds")
                ct = m.get("resolves_at") or m.get("end_time") or "unknown"
                price = m.get("current_price", "?")
                logger.info(
                    f"  Eligible: {mid[:12]}... p={price:.2f} closes in {tr:.0f}s (at {ct})"
                )

            five_min_markets = tradeable_markets

            # Build token→market index for copy bots (covers all discovered markets)
            markets_by_token = {}
            for m in markets:
                yes_tok = m.get("polymarket_token_id")
                no_tok = m.get("polymarket_no_token_id")
                if yes_tok:
                    markets_by_token[yes_tok] = m
                if no_tok:
                    markets_by_token[no_tok] = m

            # Gather signals
            price_signals = price_feed.get_signals("btc")
            sent_signals = sentiment_feed.get_signals("btc")

            new_trades = 0
            for market in five_min_markets:
                market_id = market.get("id") or market.get("market_id")
                of_signals = orderflow_feed.get_signals(market_id, api_key)

                # Polymarket YES price momentum — rate of change in the market's
                # own prediction price over the last few minutes
                yes_token = market.get("polymarket_token_id", "")
                pm_data = pm_price_feed.get_momentum(yes_token) if yes_token else {}
                pm_signals = {"pm_momentum": pm_data.get("momentum", 0.0),
                              "pm_prices": pm_data.get("prices", [])}
                if pm_data.get("fresh") and pm_data.get("prices"):
                    logger.debug(
                        f"PM momentum for {market.get('question','')[:40]}: "
                        f"{pm_data['momentum']:+.4f} prices={pm_data['prices']}"
                    )

                combined_signals = {**price_signals, **sent_signals, **of_signals, **pm_signals}

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
                            bot_mode = db.get_bot_mode(bot.name)
                            if bot_mode == "live":
                                logger.info(f"[{bot.name}] SKIP price={market.get('current_price', 0):.3f} | {signal.get('reasoning', '')}")
                            else:
                                logger.debug(f"[{bot.name}] skip | {signal.get('reasoning', '')}")
                            continue

                        result = bot.execute(signal, market)
                        traded.add(key)
                        if result.get("success"):
                            new_trades += 1
                            logger.info(f"[{bot.name}] {signal['side'].upper()} ${signal['suggested_amount']:.2f} (conf={signal['confidence']:.2f}) on {market.get('question', '')[:50]}")
                        else:
                            logger.warning(f"[{bot.name}] Trade failed on {market_id}: {result.get('reason')}")
                    except Exception as e:
                        logger.error(f"[{bot.name}] Error on {market_id}: {e}")
                        traded.add(key)

            if new_trades > 0:
                logger.info(f"Placed {new_trades} new trades this cycle")

            # --- Maker section (experimental, paper-only) ---
            # Runs after taker bots so it never blocks taker execution.
            maker_trades = 0
            for maker_bot in maker_bots:
                if run_maker_section(maker_bot, selected_market, combined_signals, traded):
                    maker_trades += 1
            if maker_trades > 0:
                maker_logger.info(f"Maker section placed {maker_trades} paper trades this cycle")

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

    # Repair one historic replay bug: older releases learned feature-less trades
    # again on every restart. Rebuild active counters once from trade ground truth.
    active_names = [b.name for b in bots]
    if db.get_arena_state("learning_rebuild_v8") != "complete":
        rebuilt = learning.rebuild_from_resolved_trades(bot_names=active_names)
        db.set_arena_state("learning_rebuild_v8", "complete")
        logger.info(
            f"Rebuilt learning from {rebuilt} resolved trades for active bots: "
            f"{active_names}"
        )
    else:
        backfilled = learning.backfill_from_resolved_trades(bot_names=active_names)
        if backfilled:
            logger.info(
                f"Backfilled learning from {backfilled} trades for active bots: "
                f"{active_names}"
            )

    main_loop(bots, api_key)


if __name__ == "__main__":
    main()
