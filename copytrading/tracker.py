"""Track top-performing Polymarket wallets."""

import json
import logging
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import db

logger = logging.getLogger(__name__)


class WalletTracker:
    def __init__(self):
        self.tracked_wallets = {}

    def add_wallet(self, address: str, label: str = None):
        """Start tracking a wallet."""
        with db.get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO copytrading_wallets (address, label)
                   VALUES (?, ?)""",
                (address, label or address[:12])
            )
        self.tracked_wallets[address] = {"label": label, "last_check": 0}
        logger.info(f"Tracking wallet: {address} ({label})")

    def remove_wallet(self, address: str):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE copytrading_wallets SET active=0 WHERE address=?", (address,)
            )
        self.tracked_wallets.pop(address, None)

    def get_wallet_positions(self, address: str, api_key: str) -> list:
        """Get positions for a tracked wallet."""
        try:
            import requests
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(
                f"{config.SIMMER_BASE_URL}/api/sdk/wallet/{address}/positions",
                headers=headers, timeout=15
            )
            if resp.status_code == 200:
                return resp.json().get("positions", [])
        except Exception as e:
            logger.error(f"Error fetching wallet {address[:12]} positions: {e}")
        return []

    def scan_all(self, api_key: str) -> dict:
        """Scan all tracked wallets for current positions."""
        results = {}
        for address in list(self.tracked_wallets):
            positions = self.get_wallet_positions(address, api_key)
            results[address] = positions
            self.tracked_wallets[address]["last_check"] = time.time()
        return results

    def get_tracked(self) -> list:
        """Get list of tracked wallets from DB."""
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM copytrading_wallets WHERE active=1"
            ).fetchall()
            return [dict(r) for r in rows]
