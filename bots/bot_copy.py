"""CopyBot — mirrors every trade made by a tracked Polymarket wallet.

Polls the Polymarket activity API every cycle, detects new trades,
and executes the same trade on our account (paper via Simmer, live via CLOB).

Usage:
    bot = CopyBot("0xABC...", label="Female-Bongo", mode="paper", max_size=5.0)
    # In arena loop:
    bot.check_and_copy(markets_by_token, api_key)

markets_by_token: dict mapping polymarket_token_id -> Simmer market dict,
built during market discovery so we can find the Simmer market_id for paper trades.

Dedup: seen {tx_hash}:{asset} keys are persisted to copytrading_trades.source_tx_hash
so we don't re-copy trades across arena restarts.
"""

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import db

logger = logging.getLogger(__name__)

ACTIVITY_API = "https://data-api.polymarket.com/activity"


class CopyBot:
    def __init__(
        self,
        wallet_address: str,
        label: str = None,
        mode: str = "paper",
        max_size: float = 5.0,
        size_fraction: float = 0.10,
        daily_loss_limit: float = None,
        max_per_cycle: int = None,
    ):
        self.wallet = wallet_address.lower()
        self.label = label or wallet_address[:16]
        self.mode = mode          # "paper" or "live"
        self.max_size = max_size  # cap per copied trade in USDC
        self.size_fraction = size_fraction  # fraction of whale's USDC size to copy
        self.name = f"copy-{self.label}"
        self.daily_loss_limit = daily_loss_limit if daily_loss_limit is not None else config.COPYTRADING_DAILY_LOSS_LIMIT
        self.max_per_cycle = max_per_cycle if max_per_cycle is not None else config.COPYTRADING_MAX_TRADES_PER_CYCLE
        self.min_price = config.COPYTRADING_MIN_PRICE
        self.max_price = getattr(config, 'COPYTRADING_MAX_PRICE', 1.0)
        self.copy_no_bets = config.COPYTRADING_COPY_NO_BETS
        self.blocked_hours = set(config.COPYTRADING_BLOCKED_HOURS_UTC)

        # Dedup: load previously seen {tx_hash}:{asset} keys from DB
        self.seen_keys: set[str] = set()
        self._load_seen_keys()
        self._monitor = None  # WalletMonitor (set via attach_monitor)
        logger.info(
            f"CopyBot [{self.label}] init: mode={mode} max=${max_size} "
            f"fraction={size_fraction:.0%} seen={len(self.seen_keys)} past trades"
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _load_seen_keys(self):
        """Seed seen_keys from DB so restarts don't re-copy old trades."""
        try:
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT source_tx_hash FROM copytrading_trades "
                    "WHERE wallet_address=? AND source_tx_hash IS NOT NULL",
                    (self.wallet,),
                ).fetchall()
                for r in rows:
                    self.seen_keys.add(r[0])
        except Exception as e:
            logger.warning(f"CopyBot [{self.label}]: could not load seen keys: {e}")

    def _get_today_losses(self) -> float:
        """Return total realized losses from copy trades today (UTC calendar day).

        Only counts resolved losing trades — wins don't count toward the limit,
        giving unlimited upside while capping downside at daily_loss_limit.
        """
        try:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(ABS(pnl)), 0) FROM trades "
                    "WHERE bot_name=? AND outcome='loss' AND date(created_at)=date('now')",
                    (self.name,),
                ).fetchone()
                return float(row[0])
        except Exception:
            return 0.0

    def _log_copy_trade(self, market_id: str, side: str, amount: float,
                        our_trade_id: str, source_key: str):
        try:
            with db.get_conn() as conn:
                conn.execute(
                    """INSERT INTO copytrading_trades
                       (wallet_address, market_id, side, amount, our_trade_id, source_tx_hash)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (self.wallet, market_id, side, amount, our_trade_id, source_key),
                )
        except Exception as e:
            logger.warning(f"CopyBot [{self.label}]: DB log failed: {e}")

    def attach_monitor(self, monitor):
        """Attach a WalletMonitor for real-time trade detection.

        When attached, check_and_copy() drains the monitor's queue instead of
        polling the activity API directly.  The monitor is pre-seeded with our
        seen_keys so it won't re-queue trades we've already processed.
        """
        self._monitor = monitor
        monitor.seed_seen_keys(self.seen_keys)
        logger.info(f"CopyBot [{self.label}]: real-time monitor attached ✓")

    # ── Activity polling ──────────────────────────────────────────────────────

    def fetch_new_trades(self) -> list[dict]:
        """Poll Polymarket activity API; return unseen trade entries."""
        try:
            resp = requests.get(
                ACTIVITY_API,
                params={"user": self.wallet, "limit": 30},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"CopyBot [{self.label}]: activity API {resp.status_code}")
                return []

            trades = resp.json()
            if not isinstance(trades, list):
                return []

            new_trades = []
            seen_this_call: set[str] = set()

            for t in trades:
                tx = t.get("transactionHash", "")
                asset = t.get("asset", "")
                key = f"{tx}:{asset}"

                if not key or key == ":":
                    continue
                if key in self.seen_keys or key in seen_this_call:
                    continue

                seen_this_call.add(key)
                new_trades.append({**t, "_key": key})

            return new_trades

        except Exception as e:
            logger.error(f"CopyBot [{self.label}]: fetch error: {e}")
            return []

    # ── Trade execution ───────────────────────────────────────────────────────

    def _execute_paper(self, market_id: str, market_question: str,
                       side: str, amount: float, reasoning: str, api_key: str):
        """Execute via Simmer paper trading."""
        try:
            resp = requests.post(
                f"{config.SIMMER_BASE_URL}/api/sdk/trade",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "market_id": market_id,
                    "side": side,
                    "amount": amount,
                    "venue": "simmer",
                    "source": f"copy:{self.label}",
                    "reasoning": reasoning,
                },
                timeout=30,
            )
            if resp.status_code in (200, 201):
                rdata = resp.json()
                trade_id = rdata.get("trade_id", "")
                shares_bought = rdata.get("shares_bought")
                # Also log to main trades table so dashboard shows it
                db.log_trade(
                    bot_name=self.name,
                    market_id=market_id,
                    market_question=market_question,
                    side=side,
                    amount=amount,
                    venue="simmer",
                    mode="paper",
                    reasoning=reasoning,
                    trade_id=trade_id,
                    shares_bought=shares_bought,
                )
                return True, trade_id
            else:
                logger.error(f"CopyBot [{self.label}]: Simmer trade failed {resp.status_code}: {resp.text[:150]}")
                return False, None
        except Exception as e:
            logger.error(f"CopyBot [{self.label}]: paper execute error: {e}")
            return False, None

    def _execute_live(self, token_id: str, market_id: str,
                      market_question: str, side: str, amount: float, reasoning: str):
        """Execute directly on Polymarket CLOB using the exact token."""
        try:
            import polymarket_client
            result = polymarket_client.place_market_order(token_id, side, amount)
            if result.get("success"):
                trade_id = result.get("order_id", "")
                db.log_trade(
                    bot_name=self.name,
                    market_id=market_id,
                    market_question=market_question,
                    side=side,
                    amount=amount,
                    venue="polymarket",
                    mode="live",
                    reasoning=reasoning,
                    trade_id=trade_id,
                    shares_bought=result.get("size"),
                )
                return True, trade_id
            else:
                logger.error(f"CopyBot [{self.label}]: CLOB trade failed: {result.get('error')}")
                return False, None
        except Exception as e:
            logger.error(f"CopyBot [{self.label}]: live execute error: {e}")
            return False, None

    def _execute_one(self, activity: dict, markets_by_token: dict, api_key: str) -> bool:
        """Execute a single copied trade. Returns True on success."""
        key = activity["_key"]
        asset = activity.get("asset", "")
        outcome_idx = activity.get("outcomeIndex", 0)
        usdc_size = activity.get("usdcSize", 0)
        price = activity.get("price", 0.5)
        title = activity.get("title", "unknown")
        outcome = activity.get("outcome", "")

        # Determine side from outcomeIndex (0=YES/Up, 1=NO/Down)
        side = "yes" if outcome_idx == 0 else "no"

        # --- Filters (mark seen so we don't retry skipped trades) ---

        # Age filter: skip trades older than 5 minutes — stale copies fill at
        # the current (often near-resolution) market price, not the whale's entry
        # price, destroying the edge. e.g. whale bought at 0.45, we copy 10 min
        # later, Simmer fills at 0.98 → +$0.02 win but -$1.00 loss = EV-negative.
        trade_ts = activity.get("timestamp", 0)
        trade_age = time.time() - trade_ts if trade_ts else 999
        if trade_age > 300:  # 5 minutes
            logger.info(
                f"CopyBot [{self.label}]: skipping — trade too old "
                f"({trade_age:.0f}s > 300s) ({title[:40]})"
            )
            self.seen_keys.add(key)
            return False

        # Hour filter: skip blocked UTC hours
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in self.blocked_hours:
            logger.info(f"CopyBot [{self.label}]: skipping — blocked hour {current_hour:02d}:xx UTC ({title[:40]})")
            self.seen_keys.add(key)
            return False

        # Side filter: skip NO bets if disabled
        if side == "no" and not self.copy_no_bets:
            logger.info(f"CopyBot [{self.label}]: skipping NO bet on {title[:40]}")
            self.seen_keys.add(key)
            return False

        # Price filter: skip entries outside [min_price, max_price]
        if price < self.min_price:
            logger.info(
                f"CopyBot [{self.label}]: skipping — price {price:.2f} < min {self.min_price:.2f} ({title[:40]})"
            )
            self.seen_keys.add(key)
            return False
        if price > self.max_price:
            logger.info(
                f"CopyBot [{self.label}]: skipping — price {price:.2f} > max {self.max_price:.2f} ({title[:40]})"
            )
            self.seen_keys.add(key)
            return False

        # Position size: fraction of whale's trade, capped
        amount = round(min(self.max_size, max(1.0, usdc_size * self.size_fraction)), 2)

        # Find the Simmer market via token lookup
        market = markets_by_token.get(asset)
        if market is None:
            trade_age = time.time() - activity.get("timestamp", 0)
            if trade_age > 120:
                # Old trade from an already-resolved market — mark seen so we stop retrying
                self.seen_keys.add(key)
                logger.debug(f"CopyBot [{self.label}]: old trade, no active market ({title[:40]})")
            else:
                # Very recent trade — market might not be indexed yet, retry next cycle
                logger.info(
                    f"CopyBot [{self.label}]: market not found yet for recent trade "
                    f"({title[:40]}), will retry"
                )
            return False

        market_id = market.get("id") or market.get("market_id")

        # Simmer fill-price guard: reject if Simmer's current market price already
        # exceeds max_price — this catches the case where Female-Bongo buys at 0.52
        # on Polymarket but Simmer has diverged to 0.98 (different AMM state).
        # Without this check we'd lock in a terrible entry: risk $0.98 to win $0.02.
        simmer_price = market.get("current_price", 0.5)
        simmer_side_price = simmer_price if side == "yes" else (1.0 - simmer_price)
        if simmer_side_price > self.max_price:
            logger.info(
                f"CopyBot [{self.label}]: skipping — Simmer fill ~{simmer_side_price:.2f} "
                f"> max {self.max_price:.2f} (whale paid {price:.2f}) ({title[:40]})"
            )
            self.seen_keys.add(key)
            return False

        reasoning = (
            f"copy:{self.label} {side} {outcome} @ {price:.2f} "
            f"(whale ${usdc_size:.2f} → us ${amount:.2f}, simmer~{simmer_side_price:.2f})"
        )

        # Always mark as seen before executing to prevent double-trade on retry
        self.seen_keys.add(key)

        if self.mode == "live":
            success, trade_id = self._execute_live(
                asset, market_id, title, side, amount, reasoning
            )
        else:
            success, trade_id = self._execute_paper(
                market_id, title, side, amount, reasoning, api_key
            )

        if success:
            self._log_copy_trade(market_id, side, amount, trade_id or "", key)
            logger.info(
                f"CopyBot [{self.label}] ✓ {side.upper()} ${amount:.2f} "
                f"(whale ${usdc_size:.2f} × {self.size_fraction:.0%}) "
                f"on {title[:50]}"
            )
            return True
        else:
            return False

    # ── Main entry point ──────────────────────────────────────────────────────

    def check_and_copy(self, markets_by_token: dict, api_key: str) -> int:
        """Check for new wallet trades and copy them. Returns number of trades placed."""
        # Prefer monitor queue (real-time, ~2-5s lag) over direct polling (~30s lag)
        if self._monitor is not None:
            new_trades = self._monitor.drain_queue()
        else:
            new_trades = self.fetch_new_trades()
        if not new_trades:
            return 0

        logger.info(f"CopyBot [{self.label}]: {len(new_trades)} new trades detected")

        # Check daily loss cap before doing anything (wins are unlimited, losses are capped)
        today_losses = self._get_today_losses()
        if today_losses >= self.daily_loss_limit:
            logger.warning(
                f"CopyBot [{self.label}]: daily loss limit reached "
                f"(${today_losses:.2f} losses / ${self.daily_loss_limit:.2f} cap), skipping cycle"
            )
            # Still mark all new trades as seen so we don't retry them tomorrow
            for trade in new_trades:
                self.seen_keys.add(trade["_key"])
            return 0

        count = 0
        for trade in new_trades:
            # Per-cycle cap
            if count >= self.max_per_cycle:
                logger.info(
                    f"CopyBot [{self.label}]: per-cycle cap hit ({self.max_per_cycle}), "
                    f"deferring {len(new_trades) - count} trades to next cycle"
                )
                break

            # Re-check losses before each trade
            today_losses = self._get_today_losses()
            if today_losses >= self.daily_loss_limit:
                logger.warning(
                    f"CopyBot [{self.label}]: daily loss limit reached mid-cycle "
                    f"(${today_losses:.2f} losses / ${self.daily_loss_limit:.2f} cap)"
                )
                break

            if self._execute_one(trade, markets_by_token, api_key):
                count += 1
                time.sleep(0.5)  # small delay between consecutive live orders

        return count

    def get_stats(self) -> dict:
        """Return copy trading performance stats for this wallet."""
        with db.get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                          ROUND(SUM(pnl), 2) as pnl
                   FROM trades WHERE bot_name=?""",
                (self.name,),
            ).fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        return {
            "wallet": self.wallet,
            "label": self.label,
            "mode": self.mode,
            "total_trades": total,
            "win_rate": wins / total if total > 0 else 0,
            "pnl": row["pnl"] or 0,
        }
