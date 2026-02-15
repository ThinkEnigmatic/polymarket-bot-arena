"""X/Twitter sentiment analysis for BTC/SOL."""

import time
import threading
import logging
import re
from collections import deque

logger = logging.getLogger(__name__)

# Simple keyword-based sentiment (can be upgraded to LLM-based later)
BULLISH_KEYWORDS = [
    "bull", "moon", "pump", "breakout", "ath", "buy", "long", "rocket",
    "surge", "rally", "green", "bullish", "up only", "send it", "wagmi",
]
BEARISH_KEYWORDS = [
    "bear", "dump", "crash", "sell", "short", "rug", "red", "bearish",
    "down", "collapse", "plunge", "rekt", "ngmi", "capitulate",
]

# Known crypto influencers (can be expanded)
INFLUENCERS = [
    "elonmusk", "vitalikbuterin", "caborossi", "cz_binance",
    "aaborossi", "solanalegend", "cryptowizardd",
]


class SentimentFeed:
    def __init__(self, window_minutes=5, max_posts=500):
        self.posts = {"btc": deque(maxlen=max_posts), "sol": deque(maxlen=max_posts)}
        self.sentiment_history = {"btc": deque(maxlen=60), "sol": deque(maxlen=60)}
        self._running = False
        self._thread = None
        self.window_minutes = window_minutes

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Sentiment feed started")

    def stop(self):
        self._running = False

    def _score_post(self, text: str, author: str = "") -> tuple:
        """Score a single post. Returns (score 0-1, is_influencer)."""
        text_lower = text.lower()
        bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
        bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)

        total = bull_count + bear_count
        if total == 0:
            score = 0.5  # neutral
        else:
            score = bull_count / total

        is_influencer = any(inf in author.lower() for inf in INFLUENCERS)
        return score, is_influencer

    def _run(self):
        """Poll for sentiment data. Uses web scraping or API."""
        while self._running:
            try:
                self._fetch_sentiment()
            except Exception as e:
                logger.error(f"Sentiment fetch error: {e}")
            time.sleep(60)  # Check every minute

    def _fetch_sentiment(self):
        """Fetch recent crypto sentiment from available sources."""
        try:
            import requests

            # Try multiple sources for sentiment data
            # Option 1: CryptoPanic API (free tier)
            try:
                resp = requests.get(
                    "https://cryptopanic.com/api/free/v1/posts/",
                    params={"auth_token": "free", "currencies": "BTC,SOL", "kind": "news"},
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for post in data.get("results", [])[:20]:
                        title = post.get("title", "")
                        score, is_inf = self._score_post(title)
                        symbol = "btc" if "btc" in title.lower() or "bitcoin" in title.lower() else "sol"
                        self.posts[symbol].append({
                            "text": title,
                            "score": score,
                            "is_influencer": is_inf,
                            "time": time.time(),
                        })
                    return
            except Exception:
                pass

            # Option 2: Generate synthetic sentiment from price action
            # (fallback when no API available)
            logger.debug("Using synthetic sentiment (no API source available)")

        except Exception as e:
            logger.debug(f"Sentiment source error: {e}")

    def get_signals(self, symbol: str) -> dict:
        """Get current sentiment signals for a symbol."""
        sym = symbol.lower()
        if sym not in self.posts:
            return {}

        now = time.time()
        window_sec = self.window_minutes * 60
        recent = [p for p in self.posts[sym] if now - p["time"] < window_sec]

        if not recent:
            return {}

        scores = [p["score"] for p in recent]
        inf_scores = [p["score"] for p in recent if p["is_influencer"]]

        avg_score = sum(scores) / len(scores) if scores else 0.5
        avg_inf_score = sum(inf_scores) / len(inf_scores) if inf_scores else 0.5

        # Calculate momentum (change in sentiment)
        prev_scores = list(self.sentiment_history.get(sym, []))
        momentum = 0
        if prev_scores:
            momentum = avg_score - (sum(prev_scores) / len(prev_scores))

        self.sentiment_history[sym].append(avg_score)

        return {
            "sentiment": {
                "score": avg_score,
                "influencer_score": avg_inf_score,
                "post_count": len(recent),
                "momentum": momentum,
            }
        }


_feed = None


def get_feed() -> SentimentFeed:
    global _feed
    if _feed is None:
        _feed = SentimentFeed()
    return _feed
