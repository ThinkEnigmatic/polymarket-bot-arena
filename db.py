"""SQLite database for all trades, bot performance, evolution history."""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
import config

DB_PATH = config.DB_PATH


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_question TEXT,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                confidence REAL,
                reasoning TEXT,
                trade_features TEXT,
                venue TEXT NOT NULL,
                mode TEXT NOT NULL,
                trade_id TEXT,
                shares_bought REAL,
                outcome TEXT,
                pnl REAL,
                resolved_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                generation INTEGER DEFAULT 0,
                lineage TEXT,
                params TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                retired_at TEXT
            );

            CREATE TABLE IF NOT EXISTS evolution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_number INTEGER NOT NULL,
                survivors TEXT NOT NULL,
                replaced TEXT NOT NULL,
                new_bots TEXT NOT NULL,
                rankings TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,
                date TEXT NOT NULL,
                trades_count INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                mode TEXT NOT NULL,
                UNIQUE(bot_name, date, mode)
            );

            CREATE TABLE IF NOT EXISTS bot_learning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,
                feature_key TEXT NOT NULL,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(bot_name, feature_key)
            );

            CREATE TABLE IF NOT EXISTS copytrading_wallets (
                address TEXT PRIMARY KEY,
                label TEXT,
                tracked_since TEXT DEFAULT (datetime('now')),
                total_trades INTEGER DEFAULT 0,
                win_rate REAL,
                total_pnl REAL DEFAULT 0,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS copytrading_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT NOT NULL,
                market_id TEXT,
                side TEXT,
                amount REAL,
                our_trade_id TEXT,
                outcome TEXT,
                pnl REAL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_trade(bot_name, market_id, side, amount, venue, mode, confidence=None,
              reasoning=None, market_question=None, trade_id=None, shares_bought=None,
              trade_features=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trades (bot_name, market_id, market_question, side, amount,
               confidence, reasoning, trade_features, venue, mode, trade_id, shares_bought)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bot_name, market_id, market_question, side, amount,
             confidence, reasoning,
             json.dumps(trade_features) if trade_features else None,
             venue, mode, trade_id, shares_bought)
        )


def resolve_trade(internal_id, outcome, pnl):
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET outcome=?, pnl=?, resolved_at=datetime('now') WHERE id=?",
            (outcome, pnl, internal_id)
        )


def get_bot_trades(bot_name, hours=None, limit=50):
    with get_conn() as conn:
        if hours:
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                "SELECT * FROM trades WHERE bot_name=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
                (bot_name, cutoff, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE bot_name=? ORDER BY created_at DESC LIMIT ?",
                (bot_name, limit)
            ).fetchall()
        return [dict(r) for r in rows]


def get_bot_performance(bot_name, hours=12):
    with get_conn() as conn:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        row = conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl
            FROM trades
            WHERE bot_name=? AND created_at>=? AND outcome IS NOT NULL
        """, (bot_name, cutoff)).fetchone()
        result = dict(row)
        result["wins"] = result["wins"] or 0
        result["losses"] = result["losses"] or 0
        total = result["wins"] + result["losses"]
        result["win_rate"] = result["wins"] / total if total > 0 else 0
        return result


def get_all_bots_performance(hours=12):
    with get_conn() as conn:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT
                bot_name,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl
            FROM trades
            WHERE created_at>=? AND outcome IS NOT NULL
            GROUP BY bot_name
        """, (cutoff,)).fetchall()
        results = {}
        for r in rows:
            d = dict(r)
            d["wins"] = d["wins"] or 0
            d["losses"] = d["losses"] or 0
            total = d["wins"] + d["losses"]
            d["win_rate"] = d["wins"] / total if total > 0 else 0
            results[d["bot_name"]] = d
        return results


def save_bot_config(bot_name, strategy_type, generation, params, lineage=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO bot_configs (bot_name, strategy_type, generation, lineage, params)
               VALUES (?, ?, ?, ?, ?)""",
            (bot_name, strategy_type, generation, lineage, json.dumps(params))
        )


def retire_bot(bot_name):
    with get_conn() as conn:
        conn.execute(
            "UPDATE bot_configs SET active=0, retired_at=datetime('now') WHERE bot_name=? AND active=1",
            (bot_name,)
        )


def get_active_bots():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_configs WHERE active=1 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def log_evolution(cycle_number, survivors, replaced, new_bots, rankings):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO evolution_events (cycle_number, survivors, replaced, new_bots, rankings)
               VALUES (?, ?, ?, ?, ?)""",
            (cycle_number, json.dumps(survivors), json.dumps(replaced),
             json.dumps(new_bots), json.dumps(rankings))
        )


def get_evolution_history(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM evolution_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_daily_loss(mode="paper"):
    with get_conn() as conn:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl), 0) as total_loss
            FROM trades
            WHERE mode=? AND date(created_at)=? AND pnl < 0 AND outcome IS NOT NULL
        """, (mode, today)).fetchone()
        return abs(dict(row)["total_loss"])


def get_bot_daily_loss(bot_name, mode="paper"):
    with get_conn() as conn:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl), 0) as total_loss
            FROM trades
            WHERE bot_name=? AND mode=? AND date(created_at)=? AND pnl < 0 AND outcome IS NOT NULL
        """, (bot_name, mode, today)).fetchone()
        return abs(dict(row)["total_loss"])


def get_dashboard_stats():
    with get_conn() as conn:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()

        # Exclude phantom trades (pnl=0 resolved from voting era)
        today_stats = conn.execute("""
            SELECT COUNT(*) as trades, COALESCE(SUM(pnl), 0) as pnl,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 AND outcome IS NOT NULL THEN 1 ELSE 0 END) as losses
            FROM trades WHERE date(created_at)=?
                AND NOT (outcome IS NOT NULL AND pnl = 0)
        """, (today,)).fetchone()

        week_stats = conn.execute("""
            SELECT COUNT(*) as trades, COALESCE(SUM(pnl), 0) as pnl,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 AND outcome IS NOT NULL THEN 1 ELSE 0 END) as losses
            FROM trades WHERE created_at>=?
                AND NOT (outcome IS NOT NULL AND pnl = 0)
        """, (week_ago,)).fetchone()

        all_stats = conn.execute("""
            SELECT COUNT(*) as trades, COALESCE(SUM(pnl), 0) as pnl,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 AND outcome IS NOT NULL THEN 1 ELSE 0 END) as losses
            FROM trades
                WHERE NOT (outcome IS NOT NULL AND pnl = 0)
        """).fetchone()

        return {
            "today": dict(today_stats),
            "week": dict(week_stats),
            "all_time": dict(all_stats),
        }


init_db()
