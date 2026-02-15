"""Mirror trades from tracked wallets via Simmer copytrading API."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import db

logger = logging.getLogger(__name__)


class TradeCopier:
    def __init__(self, tracker):
        self.tracker = tracker
        self.position_size_fraction = config.COPYTRADING_POSITION_SIZE_FRACTION

    def execute_copy(self, api_key: str, wallets: list = None, max_per_position: float = None):
        """Copy trades from tracked wallets using Simmer API."""
        if not config.COPYTRADING_ENABLED:
            logger.info("Copy trading disabled")
            return []

        addresses = wallets or [w["address"] for w in self.tracker.get_tracked()]
        if not addresses:
            logger.info("No wallets to copy")
            return []

        max_usd = max_per_position or config.get_max_position() * self.position_size_fraction
        top_n = min(10, len(addresses))

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "wallets": addresses[:config.COPYTRADING_MAX_WALLETS_TO_TRACK],
                "max_usd_per_position": max_usd,
                "top_n": top_n,
            }

            resp = requests.post(
                f"{config.SIMMER_BASE_URL}/api/sdk/copytrading/execute",
                headers=headers, json=payload, timeout=30
            )

            if resp.status_code in (200, 201):
                result = resp.json()
                trades = result.get("trades", [])
                for t in trades:
                    db.log_trade(
                        bot_name="copytrade",
                        market_id=t.get("market_id", ""),
                        market_question=t.get("market_question", ""),
                        side=t.get("side", ""),
                        amount=t.get("amount", 0),
                        venue=config.get_venue(),
                        mode=config.get_current_mode(),
                        reasoning=f"Copied from wallet {t.get('wallet', '')[:12]}",
                        trade_id=t.get("trade_id"),
                    )
                logger.info(f"Copied {len(trades)} trades from {len(addresses)} wallets")
                return trades
            else:
                logger.error(f"Copy trading failed: {resp.status_code} {resp.text[:200]}")
                return []

        except Exception as e:
            logger.error(f"Copy trading exception: {e}")
            return []

    def get_copy_stats(self) -> dict:
        """Get copy trading performance stats."""
        with db.get_conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as total, COALESCE(SUM(pnl), 0) as total_pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl <= 0 AND outcome IS NOT NULL THEN 1 ELSE 0 END) as losses
                FROM trades WHERE bot_name='copytrade'
            """).fetchone()
            d = dict(row)
            total = (d["wins"] or 0) + (d["losses"] or 0)
            d["win_rate"] = d["wins"] / total if total > 0 else 0
            return d
