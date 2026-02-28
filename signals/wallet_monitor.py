"""Real-time Polymarket wallet monitor using Polygon WebSocket.

Subscribes to newHeads on Polygon (new block ~every 2s) and immediately
polls the Polymarket activity API for the tracked wallet.  New trades
arrive in the queue within 2-5 seconds instead of the ~30s polling lag.

Falls back to polling every 15 seconds if WebSocket is unavailable.

Usage:
    monitor = WalletMonitor("0xABC...", label="Female-Bongo")
    monitor.seed_seen_keys(existing_keys_from_db)
    monitor.start()

    # In arena loop:
    trades = monitor.drain_queue()
"""

import asyncio
import json
import logging
import queue
import threading
import time

import requests

logger = logging.getLogger(__name__)

POLYGON_WS_URL = "wss://polygon-bor-rpc.publicnode.com"
ACTIVITY_API = "https://data-api.polymarket.com/activity"
FALLBACK_POLL_INTERVAL = 15    # seconds between polls when WS is silent
WS_QUIET_THRESHOLD = 30        # seconds of WS silence before fallback kicks in


class WalletMonitor:
    """Monitors a Polymarket wallet for new trades in near-real-time.

    Runs two background threads:
      - ws thread:       subscribes to Polygon newHeads, polls on every block
      - fallback thread: polls every 15s whenever the WS has been silent >30s

    New trades are placed in trade_queue for the CopyBot to consume.
    """

    def __init__(self, wallet_address: str, label: str = None):
        self.wallet = wallet_address.lower()
        self.label = label or wallet_address[:16]
        self.trade_queue: queue.Queue = queue.Queue()
        self._seen_keys: set[str] = set()
        self._seen_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._last_ws_trigger: float = 0.0   # timestamp of last WS-triggered poll

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start both monitoring threads (WebSocket + fallback)."""
        threading.Thread(
            target=self._ws_thread,
            daemon=True,
            name=f"ws-{self.label}",
        ).start()
        threading.Thread(
            target=self._fallback_thread,
            daemon=True,
            name=f"fallback-{self.label}",
        ).start()
        logger.info(
            f"WalletMonitor [{self.label}]: started for {self.wallet[:20]}... "
            f"(WS={POLYGON_WS_URL})"
        )

    def stop(self):
        """Signal both threads to stop."""
        self._stop_event.set()

    def seed_seen_keys(self, keys):
        """Pre-populate seen keys from DB to avoid re-queuing old trades on startup."""
        with self._seen_lock:
            self._seen_keys.update(keys)
        logger.debug(f"WalletMonitor [{self.label}]: seeded {len(keys)} seen keys")

    def drain_queue(self) -> list[dict]:
        """Drain and return all currently queued trades (non-blocking)."""
        trades = []
        while True:
            try:
                trades.append(self.trade_queue.get_nowait())
            except queue.Empty:
                break
        return trades

    # ── Internal polling ──────────────────────────────────────────────────────

    def _poll_activity(self) -> list[dict]:
        """Fetch Polymarket activity API; return only unseen trades."""
        try:
            resp = requests.get(
                ACTIVITY_API,
                params={"user": self.wallet, "limit": 30},
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            entries = resp.json()
            if not isinstance(entries, list):
                return []

            new_trades = []
            with self._seen_lock:
                for t in entries:
                    tx = t.get("transactionHash", "")
                    asset = t.get("asset", "")
                    key = f"{tx}:{asset}"
                    if not key or key == ":":
                        continue
                    if key in self._seen_keys:
                        continue
                    self._seen_keys.add(key)
                    new_trades.append({**t, "_key": key})

            return new_trades
        except Exception as e:
            logger.debug(f"WalletMonitor [{self.label}]: poll error: {e}")
            return []

    def _enqueue_trades(self, trades: list[dict]):
        for t in trades:
            self.trade_queue.put(t)
            side = "YES" if t.get("outcomeIndex", 0) == 0 else "NO"
            title = t.get("title", "")[:45]
            logger.info(
                f"WalletMonitor [{self.label}]: ⚡ queued {side} trade on {title!r}"
            )

    # ── WebSocket thread ──────────────────────────────────────────────────────

    def _ws_thread(self):
        """Run the asyncio WebSocket event loop in a dedicated thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_loop())
        except Exception as e:
            logger.error(f"WalletMonitor [{self.label}]: WS thread crashed: {e}")
        finally:
            loop.close()

    async def _ws_loop(self):
        """WebSocket loop — subscribes to Polygon newHeads, auto-reconnects."""
        try:
            import websockets as _ws
        except ImportError:
            logger.warning(
                f"WalletMonitor [{self.label}]: websockets library not installed — "
                "WS disabled, using polling only"
            )
            return

        retry_delay = 5
        while not self._stop_event.is_set():
            try:
                async with _ws.connect(
                    POLYGON_WS_URL,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=15,
                ) as ws:
                    # Subscribe to new block headers
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }))
                    sub_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    sub_resp = json.loads(sub_raw)
                    sub_id = sub_resp.get("result")
                    if not sub_id:
                        logger.warning(
                            f"WalletMonitor [{self.label}]: WS subscription rejected: {sub_resp}"
                        )
                        raise ValueError("No subscription ID returned")

                    logger.info(
                        f"WalletMonitor [{self.label}]: Polygon WS ✓ connected (sub={sub_id})"
                    )
                    retry_delay = 5  # reset backoff on successful connect

                    async for message in ws:
                        if self._stop_event.is_set():
                            return
                        data = json.loads(message)
                        if data.get("method") == "eth_subscription":
                            # New Polygon block → poll Polymarket activity immediately
                            self._last_ws_trigger = time.time()
                            trades = self._poll_activity()
                            if trades:
                                self._enqueue_trades(trades)

            except Exception as e:
                if not self._stop_event.is_set():
                    logger.debug(
                        f"WalletMonitor [{self.label}]: WS error ({e!r}), "
                        f"reconnecting in {retry_delay}s"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

    # ── Fallback polling thread ───────────────────────────────────────────────

    def _fallback_thread(self):
        """Poll every FALLBACK_POLL_INTERVAL seconds when WS has been silent."""
        while not self._stop_event.is_set():
            self._stop_event.wait(FALLBACK_POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            # Only poll if WS hasn't triggered recently (WS might be active)
            if time.time() - self._last_ws_trigger > WS_QUIET_THRESHOLD:
                trades = self._poll_activity()
                if trades:
                    self._enqueue_trades(trades)
