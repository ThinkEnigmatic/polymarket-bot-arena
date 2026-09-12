"""Adaptive learning engine for trading bots.

Each bot tracks win rates across feature dimensions (market price bucket,
price momentum, hour of day). After every resolved trade, the features
that were present get their win/loss counts updated. When making new
decisions, the bot queries its learned win rates to bias yes/no.
"""

import math
import logging
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db

logger = logging.getLogger(__name__)
EASTERN_TZ = ZoneInfo("America/New_York")

# Feature buckets
PRICE_BUCKETS = [
    ("price_very_low", 0.0, 0.30),    # Market price < 30% → strong yes lean
    ("price_low", 0.30, 0.45),
    ("price_neutral", 0.45, 0.55),
    ("price_high", 0.55, 0.70),
    ("price_very_high", 0.70, 1.01),   # Market price > 70% → strong no lean
]

MOMENTUM_BUCKETS = [
    ("mom_strong_down", -999, -0.003),
    ("mom_down", -0.003, -0.001),
    ("mom_flat", -0.001, 0.001),
    ("mom_up", 0.001, 0.003),
    ("mom_strong_up", 0.003, 999),
]

HOUR_BUCKETS = [
    ("hour_morning", 6, 12),     # 6am-12pm ET
    ("hour_afternoon", 12, 17),  # 12pm-5pm ET
    ("hour_evening", 17, 22),    # 5pm-10pm ET
    ("hour_night", 22, 6),       # 10pm-6am ET (wraps)
]

# New: Market context buckets
VOLUME_BUCKETS = [
    ("vol_low", 0, 500),
    ("vol_mid", 500, 2500),
    ("vol_high", 2500, 9999999),
]

TIME_BUCKETS = [
    ("time_early", 240, 9999),   # >4 min remaining
    ("time_mid", 120, 240),      # 2-4 min remaining
    ("time_late", 0, 120),       # <2 min remaining
]


def extract_features(market_price, price_momentum, hour_et=None, volume=None, time_rem=None):
    """Extract feature keys from market conditions.

    Args:
        market_price: current YES price (0.0-1.0)
        price_momentum: recent BTC price change as fraction (e.g. 0.002 = +0.2%)
        hour_et: hour of day in ET (0-23), auto-detected if None
        volume: 24h volume of the market
        time_rem: seconds remaining until resolution

    Returns:
        list of feature key strings
    """
    features = []

    # Price bucket
    for name, lo, hi in PRICE_BUCKETS:
        if lo <= market_price < hi:
            features.append(name)
            break

    # Momentum bucket
    for name, lo, hi in MOMENTUM_BUCKETS:
        if lo <= price_momentum < hi:
            features.append(name)
            break

    # Hour bucket
    if hour_et is None:
        hour_et = datetime.now(EASTERN_TZ).hour
    for name, start, end in HOUR_BUCKETS:
        if start < end:
            if start <= hour_et < end:
                features.append(name)
                break
        else:  # wraps midnight
            if hour_et >= start or hour_et < end:
                features.append(name)
                break

    # Volume bucket
    if volume is not None:
        for name, lo, hi in VOLUME_BUCKETS:
            if lo <= volume < hi:
                features.append(name)
                break

    # Time bucket
    if time_rem is not None:
        for name, lo, hi in TIME_BUCKETS:
            if lo <= time_rem < hi:
                features.append(name)
                break

    return features


def get_learned_bias(bot_name, features, prior_yes=0.5):
    """Query learned win rates for a set of features and return a yes bias.

    Uses Bayesian updating: starts with prior, adjusts based on observed
    win rates for each feature. More observations = stronger pull.

    Args:
        bot_name: which bot's learning to query
        features: list of feature keys from extract_features()
        prior_yes: starting bias toward yes (0.5 = neutral)

    Returns:
        float 0.0-1.0 representing how much to lean yes
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT feature_key, wins, losses FROM bot_learning WHERE bot_name=?",
            (bot_name,)
        ).fetchall()

    learned = {r["feature_key"]: (r["wins"], r["losses"]) for r in rows}

    # Start with prior
    log_odds = math.log(prior_yes / (1 - prior_yes)) if 0 < prior_yes < 1 else 0

    for feat in features:
        if feat not in learned:
            continue
        wins, losses = learned[feat]
        total = wins + losses
        if total < 2:
            continue  # Need at least 2 observations

        # Observed win rate for YES side trades on this feature
        feat_wr = (wins + 1) / (total + 2)  # Laplace smoothing

        # Strength of evidence scales with sqrt(observations)
        # Stronger pull: ramp up faster so bots adapt quickly
        strength = min(math.sqrt(total) * 0.5, 3.0)

        # Pull log-odds toward observed rate
        feat_log_odds = math.log(feat_wr / (1 - feat_wr))
        log_odds += feat_log_odds * strength * 0.35

    # Convert back to probability
    yes_bias = 1.0 / (1.0 + math.exp(-log_odds))
    return max(0.05, min(0.95, yes_bias))


def record_outcome(bot_name, features, side, won):
    """Update learning table after a trade resolves.

    We track: for each feature, how often did betting YES win?
    If side='yes' and won → increment wins
    If side='no' and won → increment losses (YES lost)
    """
    with db.get_conn() as conn:
        for feat in features:
            if side == "yes":
                if won:
                    conn.execute("""
                        INSERT INTO bot_learning (bot_name, feature_key, wins, losses)
                        VALUES (?, ?, 1, 0)
                        ON CONFLICT(bot_name, feature_key)
                        DO UPDATE SET wins=wins+1, updated_at=datetime('now')
                    """, (bot_name, feat))
                else:
                    conn.execute("""
                        INSERT INTO bot_learning (bot_name, feature_key, wins, losses)
                        VALUES (?, ?, 0, 1)
                        ON CONFLICT(bot_name, feature_key)
                        DO UPDATE SET losses=losses+1, updated_at=datetime('now')
                    """, (bot_name, feat))
            else:  # side == "no"
                if won:
                    # NO won → YES lost
                    conn.execute("""
                        INSERT INTO bot_learning (bot_name, feature_key, wins, losses)
                        VALUES (?, ?, 0, 1)
                        ON CONFLICT(bot_name, feature_key)
                        DO UPDATE SET losses=losses+1, updated_at=datetime('now')
                    """, (bot_name, feat))
                else:
                    # NO lost → YES won
                    conn.execute("""
                        INSERT INTO bot_learning (bot_name, feature_key, wins, losses)
                        VALUES (?, ?, 1, 0)
                        ON CONFLICT(bot_name, feature_key)
                        DO UPDATE SET wins=wins+1, updated_at=datetime('now')
                    """, (bot_name, feat))


def extract_features_from_reasoning(reasoning, hour_et=None):
    """Try to extract features from the reasoning text of old trades.

    Parses v4-format reasoning like:
        price=0.50 edge=+0.006 mom=+0.001 strat=+0.000 learn=+0.020(w=60%) => yes conf=0.03
    Also handles:
        Forced from hold: market_price=0.505
    """
    import re
    if not reasoning:
        return None

    market_price = None
    momentum = None

    # v4 format: price=X.XX ... mom=+X.XXX
    m = re.search(r'price=([\d.]+)', reasoning)
    if m:
        market_price = float(m.group(1))
    mom_m = re.search(r'mom=([+-]?[\d.]+)', reasoning)
    if mom_m:
        momentum = float(mom_m.group(1))

    # Older format: market_price=X.XX
    if market_price is None:
        m = re.search(r'market_price=([\d.]+)', reasoning)
        if m:
            market_price = float(m.group(1))

    if market_price is not None:
        return extract_features(market_price, momentum or 0.0, hour_et=hour_et)
    return None


def backfill_from_resolved_trades(bot_names=None):
    """Backfill bot_learning from resolved trades that have no trade_features.

    Parses market price from reasoning text to reconstruct features.
    Only processes trades with outcome='win' or 'loss'.
    If bot_names is provided, only backfill for those bots.
    Returns the number of trades backfilled.
    """
    with db.get_conn() as conn:
        if bot_names is not None:
            if not bot_names:
                return 0  # Empty list = nothing to backfill
            placeholders = ",".join("?" for _ in bot_names)
            rows = conn.execute(f"""
                SELECT id, bot_name, side, outcome, reasoning, created_at
                FROM trades
                WHERE outcome IN ('win', 'loss')
                  AND trade_features IS NULL
                  AND reasoning IS NOT NULL
                  AND bot_name IN ({placeholders})
            """, bot_names).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, bot_name, side, outcome, reasoning, created_at
                FROM trades
                WHERE outcome IN ('win', 'loss')
                  AND trade_features IS NULL
                  AND reasoning IS NOT NULL
            """).fetchall()

    count = 0
    for r in rows:
        hour_et = None
        try:
            created_at = datetime.strptime(
                r["created_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            hour_et = created_at.astimezone(EASTERN_TZ).hour
        except (TypeError, ValueError):
            pass

        features = extract_features_from_reasoning(
            r["reasoning"], hour_et=hour_et
        )
        if not features:
            # Last resort: use hour from created_at as the only feature
            try:
                if hour_et is None:
                    raise ValueError("invalid created_at")
                features = extract_features(0.5, 0.0, hour_et)  # neutral price bucket
            except ValueError:
                continue

        won = r["outcome"] == "win"
        record_outcome(r["bot_name"], features, r["side"], won)
        # Persist reconstructed features so this trade is never learned twice
        # on subsequent process restarts.
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE trades SET trade_features=? WHERE id=?",
                (json.dumps(features), r["id"]),
            )
        count += 1

    if count > 0:
        logger.info(f"Backfilled learning from {count} resolved trades")
    return count


def rebuild_from_resolved_trades(bot_names=None):
    """Rebuild learning counters once from resolved-trade ground truth.

    Older versions replayed feature-less trades on every restart, inflating
    their influence. Rebuilding removes those duplicate counts and makes each
    resolved trade contribute exactly once.
    """
    params = []
    where = "outcome IN ('win', 'loss')"
    if bot_names is not None:
        if not bot_names:
            return 0
        placeholders = ",".join("?" for _ in bot_names)
        where += f" AND bot_name IN ({placeholders})"
        params.extend(bot_names)

    with db.get_conn() as conn:
        if bot_names is None:
            conn.execute("DELETE FROM bot_learning")
        else:
            placeholders = ",".join("?" for _ in bot_names)
            conn.execute(
                f"DELETE FROM bot_learning WHERE bot_name IN ({placeholders})",
                bot_names,
            )
        rows = conn.execute(
            f"""SELECT id, bot_name, side, outcome, reasoning,
                       trade_features, created_at
                FROM trades WHERE {where}
                ORDER BY id""",
            params,
        ).fetchall()

    rebuilt = 0
    for row in rows:
        features = None
        if row["trade_features"]:
            try:
                features = json.loads(row["trade_features"])
            except (json.JSONDecodeError, TypeError):
                pass

        if not features:
            hour_et = None
            try:
                created_at = datetime.strptime(
                    row["created_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                hour_et = created_at.astimezone(EASTERN_TZ).hour
            except (TypeError, ValueError):
                pass
            features = extract_features_from_reasoning(
                row["reasoning"], hour_et=hour_et
            )

        if not features:
            continue

        record_outcome(
            row["bot_name"], features, row["side"], row["outcome"] == "win"
        )
        if not row["trade_features"]:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE trades SET trade_features=? WHERE id=?",
                    (json.dumps(features), row["id"]),
                )
        rebuilt += 1

    return rebuilt


def get_bot_learning_summary(bot_name):
    """Get a summary of what the bot has learned."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT feature_key, wins, losses FROM bot_learning WHERE bot_name=? ORDER BY (wins+losses) DESC",
            (bot_name,)
        ).fetchall()

    summary = []
    for r in rows:
        total = r["wins"] + r["losses"]
        wr = r["wins"] / total if total > 0 else 0.5
        summary.append({
            "feature": r["feature_key"],
            "wins": r["wins"],
            "losses": r["losses"],
            "total": total,
            "yes_win_rate": round(wr, 3),
        })
    return summary
