"""Regression tests for money-at-risk and market-selection safeguards."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["BOT_ARENA_DB_PATH"] = os.path.join(_TEMP_DIR.name, "arena-test.db")

import arena
import config
import db
import learning
import polymarket_client
from bots.base_bot import BaseBot
from bots.bot_hybrid import HybridBot
from bots.bot_meanrev_sl import MeanRevSLBot
from bots.bot_meanrev_tp import MeanRevTPBot
from bots.bot_momentum import MomentumBot
from py_clob_client.clob_types import MarketOrderArgs, OrderType


class DummyBot(BaseBot):
    def __init__(self, name="dummy"):
        super().__init__(name, "momentum", {"threshold": 1.0})

    def analyze(self, market, signals):
        return {"action": "hold", "side": "yes", "confidence": 0}


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM bot_configs")

    def _resolve_latest(self, outcome, pnl):
        with db.get_conn() as conn:
            trade_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
        db.resolve_trade(trade_id, outcome, pnl)

    def test_pending_stake_and_realized_losses_count_toward_risk(self):
        db.log_trade("alpha", "m1", "yes", 10, "polymarket", "live")
        db.log_trade("alpha", "m2", "yes", 5, "polymarket", "live")
        self._resolve_latest("loss", -5)
        db.log_trade("alpha", "m3", "yes", 3, "polymarket", "live")
        self._resolve_latest("win", 2)
        db.log_trade("beta", "m4", "yes", 7, "polymarket", "live")

        self.assertEqual(db.get_bot_daily_risk("alpha", "live"), 15)
        self.assertEqual(db.get_total_daily_risk("live"), 22)
        self.assertEqual(db.get_total_daily_risk("paper"), 0)

    def test_live_bot_uses_live_limits_even_when_global_mode_is_paper(self):
        bot = DummyBot()
        signal = {"side": "yes", "confidence": 0.8, "suggested_amount": 30}
        market = {"id": "m1", "current_price": 0.5, "polymarket_token_id": "yes"}

        with (
            patch.object(db, "get_bot_mode", return_value="live"),
            patch.object(db, "get_bot_daily_risk", return_value=0),
            patch.object(db, "get_total_daily_risk", return_value=0),
            patch.object(bot, "_execute_live", return_value={"success": True}) as execute,
        ):
            config.set_trading_mode("paper")
            result = bot.execute(signal, market)

        self.assertTrue(result["success"])
        self.assertEqual(execute.call_args.args[2], config.LIVE_MAX_POSITION)

    def test_order_is_clamped_to_remaining_daily_risk_budget(self):
        bot = DummyBot()
        signal = {"side": "yes", "confidence": 0.8, "suggested_amount": 10}
        market = {"id": "m1", "current_price": 0.5, "polymarket_token_id": "yes"}

        with (
            patch.object(db, "get_bot_mode", return_value="live"),
            patch.object(db, "get_bot_daily_risk", return_value=48.741),
            patch.object(db, "get_total_daily_risk", return_value=80),
            patch.object(bot, "_execute_live", return_value={"success": True}) as execute,
        ):
            bot.execute(signal, market)

        self.assertEqual(execute.call_args.args[2], 1.25)

    def test_stale_expiry_never_hides_a_live_position(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_conn() as conn:
            for mode in ("paper", "live"):
                conn.execute(
                    """INSERT INTO trades
                       (bot_name, market_id, side, amount, venue, mode, created_at)
                       VALUES (?, ?, 'yes', 5, ?, ?, ?)""",
                    (f"{mode}-bot", f"{mode}-market", mode, mode, old),
                )

        self.assertEqual(arena.expire_stale_trades(), 1)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT mode, outcome FROM trades ORDER BY mode"
            ).fetchall()
        outcomes = {row["mode"]: row["outcome"] for row in rows}
        self.assertIsNone(outcomes["live"])
        self.assertEqual(outcomes["paper"], "expired")


class MarketSelectionTests(unittest.TestCase):
    def test_discovery_rejects_generic_and_fifteen_minute_markets(self):
        response = Mock(status_code=200)
        response.json.return_value = [
            {"id": "five", "question": "Bitcoin Up or Down - 10:00AM-10:05AM ET"},
            {"id": "fifteen", "question": "Bitcoin Up or Down - 10:00AM-10:15AM ET"},
            {"id": "hour", "question": "Bitcoin Up or Down this hour"},
            {"id": "explicit", "question": "BTC Up or Down - 5 minute market"},
            {"id": "other", "question": "ETH Up or Down - 10:00AM-10:05AM ET"},
        ]
        with patch("requests.get", return_value=response):
            markets = arena.discover_markets("test-key")
        self.assertEqual({market["id"] for market in markets}, {"five", "explicit"})

    def test_only_the_current_five_minute_window_is_tradeable(self):
        now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

        def market(mid, seconds, *, valid=True):
            end = now + timedelta(seconds=seconds)
            return {
                "id": mid,
                "resolves_at": end.isoformat() if valid else "not-a-time",
            }

        candidates = [
            market("current", 250),
            market("too-late", 30),
            market("future", 400),
            market("invalid", 100, valid=False),
        ]
        eligible, late, future, invalid = arena.filter_tradeable_markets(candidates, now)

        self.assertEqual([item["id"] for item in eligible], ["current"])
        self.assertEqual((late, future, invalid), (1, 1, 1))
        self.assertEqual(eligible[0]["window_age_seconds"], 50)


class LearningTests(unittest.TestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM bot_learning")

    def test_rebuild_removes_inflated_learning_counts(self):
        features = ["price_high", "mom_up", "hour_morning"]
        db.log_trade(
            "alpha", "m1", "yes", 5, "simmer", "paper",
            trade_features=features,
        )
        with db.get_conn() as conn:
            trade_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
            conn.execute(
                """INSERT INTO bot_learning
                   (bot_name, feature_key, wins, losses)
                   VALUES ('alpha', 'price_high', 99, 99)"""
            )
        db.resolve_trade(trade_id, "win", 4)

        rebuilt = learning.rebuild_from_resolved_trades(["alpha"])
        summary = {
            item["feature"]: item for item in learning.get_bot_learning_summary("alpha")
        }

        self.assertEqual(rebuilt, 1)
        self.assertEqual(summary["price_high"]["wins"], 1)
        self.assertEqual(summary["price_high"]["losses"], 0)

    def test_backfill_is_idempotent_across_restarts(self):
        db.log_trade(
            "alpha", "m1", "yes", 5, "simmer", "paper",
            reasoning="price=0.60 edge=+0.01 mom=+0.002",
        )
        with db.get_conn() as conn:
            trade_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
        db.resolve_trade(trade_id, "win", 3)

        first = learning.backfill_from_resolved_trades(["alpha"])
        second = learning.backfill_from_resolved_trades(["alpha"])

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        summary = learning.get_bot_learning_summary("alpha")
        self.assertTrue(summary)
        self.assertTrue(all(item["wins"] + item["losses"] == 1 for item in summary))


class ExecutionTests(unittest.TestCase):
    def test_market_buy_is_fok_and_uses_actual_fill_amounts(self):
        client = Mock()
        client.get_order_book.return_value = SimpleNamespace(
            asks=[SimpleNamespace(price="0.90"), SimpleNamespace(price="0.54")]
        )
        client.create_market_order.return_value = "signed"
        client.post_order.return_value = {
            "success": True,
            "status": "matched",
            "orderID": "order-1",
            "makingAmount": "5000000",
            "takingAmount": "9250000",
        }

        with patch.object(polymarket_client, "get_client", return_value=client):
            result = polymarket_client.place_market_order(
                "token", "yes", 5, max_price=0.56
            )

        args = client.create_market_order.call_args.args[0]
        self.assertIsInstance(args, MarketOrderArgs)
        self.assertEqual(args.order_type, OrderType.FOK)
        self.assertEqual(args.price, 0.56)
        client.post_order.assert_called_once_with("signed", OrderType.FOK)
        self.assertTrue(result["success"])
        self.assertEqual(result["amount_spent"], 5)
        self.assertEqual(result["size"], 9.25)
        self.assertAlmostEqual(result["price"], 5 / 9.25)

    def test_unexpected_resting_fok_is_cancelled_and_not_reported_as_fill(self):
        client = Mock()
        client.get_order_book.return_value = SimpleNamespace(
            asks=[SimpleNamespace(price="0.50")]
        )
        client.create_market_order.return_value = "signed"
        client.post_order.return_value = {
            "success": True,
            "status": "live",
            "orderID": "order-2",
        }

        with patch.object(polymarket_client, "get_client", return_value=client):
            result = polymarket_client.place_market_order("token", "yes", 5)

        self.assertFalse(result["success"])
        client.cancel.assert_called_once_with("order-2")


class EvolutionAndExitTests(unittest.TestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM bot_configs")
            conn.execute("DELETE FROM evolution_events")

    def test_profitable_strategy_reproduces_instead_of_loser_type(self):
        winner = MomentumBot(name="winner")
        loser = HybridBot(name="loser")
        winner.get_performance = Mock(return_value={
            "total_pnl": 12, "avg_pnl": 0.6, "win_rate": 0.60, "total_trades": 20,
        })
        loser.get_performance = Mock(return_value={
            "total_pnl": -8, "avg_pnl": -0.4, "win_rate": 0.75, "total_trades": 20,
        })

        evolved = arena.run_evolution([winner, loser], cycle_number=1)

        self.assertEqual(len(evolved), 2)
        self.assertTrue(all(isinstance(bot, MomentumBot) for bot in evolved))

    def test_synthetic_stop_loss_and_take_profit_are_disabled(self):
        self.assertIsNone(MeanRevSLBot().exit_strategy)
        self.assertIsNone(MeanRevTPBot().exit_strategy)
        monitor = arena.PositionMonitorThread("key")
        fake = SimpleNamespace(name="unsafe", exit_strategy="stop_loss")
        monitor.update_bots([fake])
        self.assertEqual(monitor._bots, {})


if __name__ == "__main__":
    unittest.main()
