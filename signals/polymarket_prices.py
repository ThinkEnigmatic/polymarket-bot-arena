"""Polymarket in-market price momentum signal.

Fetches real price history for active BTC Up/Down markets directly from
Polymarket's CLOB history endpoint. This captures how the prediction
market ITSELF is trending, independent of BTC spot price on Binance.

Why this matters:
  - Binance BTC momentum tells us where BTC is going
  - Polymarket momentum tells us where *traders in this specific market*
    are positioning — which directly predicts YES resolution
  - They can diverge: BTC flat but smart money flooding YES side = strong
    YES signal that Binance alone would miss

Signal: pm_momentum in [-0.15, +0.15]
  > 0  → YES price trending up  → lean YES
  < 0  → YES price trending down → lean NO
"""

import logging
import time
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

PRICE_HISTORY_URL = "https://clob.polymarket.com/prices-history"
CACHE_TTL = 20          # seconds — refresh every ~20s (one trade interval)
LOOKBACK_POINTS = 5     # number of recent price points to measure momentum over
MAX_SIGNAL = 0.15       # clamp output to [-0.15, +0.15]
SCALE = 80.0            # amplifier: 0.01 price move → 0.8 signal units (before clamp)


class PolymarketPriceFeed:
    def __init__(self):
        self._cache: dict[str, dict] = {}   # token_id → {ts, momentum, prices}
        self._lock = threading.Lock()

    def _fetch_history(self, token_id: str) -> list[dict]:
        """Fetch recent price history from Polymarket CLOB."""
        try:
            resp = requests.get(
                PRICE_HISTORY_URL,
                params={"market": token_id, "interval": "1m", "fidelity": 10},
                timeout=8,
            )
            if resp.status_code == 200:
                return resp.json().get("history", [])
        except Exception as e:
            logger.debug(f"PM price history fetch error ({token_id[:20]}...): {e}")
        return []

    def get_momentum(self, token_id: str) -> dict:
        """Return price momentum signal for a YES token.

        Returns dict with keys:
          momentum   float  [-0.15, +0.15], positive = YES trending up
          prices     list   recent YES prices (newest last)
          fresh      bool   True if data was fetched this call
        """
        if not token_id:
            return {"momentum": 0.0, "prices": [], "fresh": False}

        now = time.time()

        with self._lock:
            cached = self._cache.get(token_id)
            if cached and (now - cached["ts"]) < CACHE_TTL:
                return {**cached, "fresh": False}

        # Fetch outside lock to avoid blocking other callers
        history = self._fetch_history(token_id)

        if len(history) < 2:
            return {"momentum": 0.0, "prices": [], "fresh": bool(history)}

        # Most recent prices (newest last)
        prices = [pt["p"] for pt in history[-LOOKBACK_POINTS:]]

        # Momentum: slope from oldest to newest in our window
        if len(prices) >= 2:
            delta = prices[-1] - prices[0]
            n_steps = max(1, len(prices) - 1)
            momentum_raw = delta / n_steps          # per-step price change
            momentum = max(-MAX_SIGNAL, min(MAX_SIGNAL, momentum_raw * SCALE))
        else:
            momentum = 0.0

        result = {"momentum": momentum, "prices": prices, "ts": now, "fresh": True}

        with self._lock:
            self._cache[token_id] = result

        logger.debug(
            f"PM momentum [{token_id[:16]}...]: "
            f"prices={[f'{p:.3f}' for p in prices]} → {momentum:+.4f}"
        )
        return result

    def clear(self, token_id: str = None):
        with self._lock:
            if token_id:
                self._cache.pop(token_id, None)
            else:
                self._cache.clear()


_feed: Optional[PolymarketPriceFeed] = None


def get_feed() -> PolymarketPriceFeed:
    global _feed
    if _feed is None:
        _feed = PolymarketPriceFeed()
    return _feed
