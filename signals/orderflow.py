"""Polymarket order book / CLOB signals."""

import logging
import time
import threading

logger = logging.getLogger(__name__)


class OrderflowFeed:
    def __init__(self):
        self._cache = {}
        self._running = False

    def start(self):
        self._running = True
        logger.info("Orderflow feed started")

    def stop(self):
        self._running = False

    def get_signals(self, market_id: str, api_key: str = None) -> dict:
        """Get order flow signals for a specific market."""
        if not market_id or not api_key:
            return {}

        try:
            import requests
            from pathlib import Path
            import json
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import config

            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(
                f"{config.SIMMER_BASE_URL}/api/sdk/context/{market_id}",
                headers=headers, timeout=10
            )

            if resp.status_code == 200:
                ctx = resp.json()
                return {
                    "orderflow": {
                        "current_probability": ctx.get("current_probability", 0.5),
                        "volume_24h": ctx.get("volume_24h", 0),
                        "time_to_resolution": ctx.get("time_to_resolution_seconds", 0),
                        "warnings": ctx.get("warnings", []),
                    }
                }
        except Exception as e:
            logger.debug(f"Orderflow fetch error: {e}")

        return {}


_feed = None


def get_feed() -> OrderflowFeed:
    global _feed
    if _feed is None:
        _feed = OrderflowFeed()
    return _feed
