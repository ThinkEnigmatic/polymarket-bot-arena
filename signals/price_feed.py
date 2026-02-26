"""Real-time BTC/SOL price data from Binance WebSocket."""

import json
import time
import threading
import logging
from collections import deque

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws"
SYMBOLS = {"btc": "btcusdt", "sol": "solusdt"}


class PriceFeed:
    def __init__(self, max_candles=100):
        self.prices = {sym: deque(maxlen=max_candles) for sym in SYMBOLS}
        self.volumes = {sym: deque(maxlen=max_candles) for sym in SYMBOLS}
        self.latest = {sym: 0.0 for sym in SYMBOLS}
        self._last_update = {sym: 0.0 for sym in SYMBOLS}
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Price feed started")

    def stop(self):
        self._running = False

    def _run(self):
        import websocket

        streams = "/".join(f"{s}@kline_1m" for s in SYMBOLS.values())
        url = f"{BINANCE_WS}/{streams}"

        while self._running:
            try:
                ws = websocket.WebSocket()
                ws.settimeout(10)
                ws.connect(url)
                logger.info(f"Connected to Binance WS: {url}")

                while self._running:
                    try:
                        raw = ws.recv()
                    except Exception:
                        break

                    try:
                        msg = json.loads(raw)
                        kline = msg.get("k", {})
                        symbol = kline.get("s", "").lower()
                        close = float(kline.get("c", 0))
                        volume = float(kline.get("v", 0))
                        is_closed = kline.get("x", False)

                        # Map back to our symbol names
                        for name, binance_sym in SYMBOLS.items():
                            if symbol == binance_sym:
                                self.latest[name] = close
                                self._last_update[name] = time.time()
                                if is_closed:
                                    self.prices[name].append(close)
                                    self.volumes[name].append(volume)
                                break
                    except (KeyError, ValueError):
                        continue

                ws.close()
            except Exception as e:
                logger.error(f"Price feed error: {e}")
                time.sleep(5)

    def get_signals(self, symbol: str) -> dict:
        """Get current price signals for a symbol."""
        sym = symbol.lower()
        if sym not in self.prices:
            return {"prices": [], "volumes": [], "latest": 0}

        stale = (time.time() - self._last_update.get(sym, 0)) > 60
        return {
            "prices": list(self.prices[sym]),
            "volumes": list(self.volumes[sym]),
            "latest": self.latest.get(sym, 0),
            "stale": stale,
        }


# Singleton
_feed = None


def get_feed() -> PriceFeed:
    global _feed
    if _feed is None:
        _feed = PriceFeed()
    return _feed
