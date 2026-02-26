"""Bot: BTC 5-min Maker — passive limit-order market-maker for BTC 5-min markets.

Strategy
--------
Instead of crossing the spread (taker), this bot posts resting limit orders on
both sides (or the directional side) of the BTC 5-min YES/NO book.  It earns
the maker rebate when matched.

In **paper mode** (default) the bot simulates the fill via Simmer exactly like
all other bots — the "maker" terminology reflects the *intent* of the strategy
(edge via spread, not via directional prediction).  In paper mode there is no
actual limit-order book, so the bot sends a normal Simmer trade using the
computed bid/ask mid rather than market price.

In **live mode** the bot posts real GTC limit orders to the Polymarket CLOB
via ``polymarket_client.place_limit_order``, then tracks open orders and
cancels any that are still resting when the market closes.

Parameters
----------
spread_ticks       : int   — half-spread in ticks (1 tick = $0.01)
size_shares        : float — shares per side posted to the book
directional_weight : float — 0 = pure neutral market-making;
                             1 = only post in the directionally favoured side
min_edge_bps       : int   — minimum edge in basis points to bother quoting
max_open_orders    : int   — cancel oldest orders if this many are already open
"""

from bots.base_bot import BaseBot

DEFAULT_PARAMS = {
    "spread_ticks": 2,         # Half-spread: 2 ticks = ±$0.02 around mid
    "size_shares": 10.0,       # Shares per limit order
    "directional_weight": 0.4, # Lean slightly directional (0=neutral, 1=full)
    "min_edge_bps": 50,        # 50 bps minimum mid-to-fair-value edge to quote
    "max_open_orders": 4,      # Max concurrent live orders before cancelling old ones
    "position_size_pct": 0.05, # Fallback size (% of max position) for paper mode
    "lookback_candles": 3,     # BTC candles used for directional lean
}


class BtcMakerBot(BaseBot):
    """Passive limit-order market maker for BTC 5-minute prediction markets."""

    strategy_type = "btc_maker"

    def __init__(self, name="btc-maker-v1", params=None, generation=0, lineage=None):
        super().__init__(
            name=name,
            strategy_type="btc_maker",
            params=params or DEFAULT_PARAMS.copy(),
            generation=generation,
            lineage=lineage,
        )
        # Track live order IDs so we can cancel stale orders later.
        # {market_id: [order_id, …]}
        self._open_orders: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Core analysis — computes mid-price edge and directional lean
    # ------------------------------------------------------------------

    def analyze(self, market: dict, signals: dict) -> dict:
        """Compute maker quote parameters.

        Returns a signal dict with extra maker-specific fields:
            maker_bid, maker_ask, maker_mid, maker_side
        """
        p = self.strategy_params
        prices = signals.get("prices", [])
        market_price = market.get("current_price", 0.5)

        # --- Directional lean from recent BTC candles ---
        momentum = 0.0
        lb = p["lookback_candles"]
        if len(prices) >= lb and prices[-lb] > 0:
            momentum = (prices[-1] - prices[-lb]) / prices[-lb]
        # Clamp to ±1%
        momentum = max(-0.01, min(0.01, momentum))

        # Fair value: blend market price with directional signal
        dw = p["directional_weight"]
        # momentum > 0 → BTC going up → YES more likely → fair > market_price
        fair_value = market_price + dw * momentum * 10  # scale momentum to price units
        fair_value = max(0.05, min(0.95, fair_value))

        # Tick grid
        tick = 0.01
        half_spread = p["spread_ticks"] * tick
        maker_bid = round(max(0.01, fair_value - half_spread), 2)
        maker_ask = round(min(0.99, fair_value + half_spread), 2)

        # Edge check: is the spread meaningful vs the current book?
        edge_bps = abs(fair_value - market_price) * 10000
        min_edge = p["min_edge_bps"]
        if edge_bps < min_edge and abs(momentum) < 0.001:
            return {
                "action": "hold",
                "side": "yes",
                "confidence": 0.0,
                "reasoning": (
                    f"Edge too thin: {edge_bps:.0f}bps < {min_edge}bps, "
                    f"market_price={market_price:.2f}"
                ),
                "maker_bid": maker_bid,
                "maker_ask": maker_ask,
                "maker_mid": fair_value,
                "maker_side": "both",
            }

        # Directional lean: if momentum is positive lean YES (post bid),
        # if negative lean NO (post ask on YES == posting bid on NO).
        if momentum > 0.002:
            maker_side = "yes"
            conf = min(0.85, edge_bps / 1000 + abs(momentum) * 50)
        elif momentum < -0.002:
            maker_side = "no"
            conf = min(0.85, edge_bps / 1000 + abs(momentum) * 50)
        else:
            maker_side = "both"
            conf = min(0.60, edge_bps / 1000)

        import config
        amount = config.get_max_position() * p["position_size_pct"]

        return {
            "action": "buy",
            "side": "yes" if maker_side in ("yes", "both") else "no",
            "confidence": conf,
            "reasoning": (
                f"Maker: fair={fair_value:.3f} bid={maker_bid:.2f} ask={maker_ask:.2f} "
                f"edge={edge_bps:.0f}bps mom={momentum:+.4f} lean={maker_side}"
            ),
            "suggested_amount": amount,
            "maker_bid": maker_bid,
            "maker_ask": maker_ask,
            "maker_mid": fair_value,
            "maker_side": maker_side,
        }

    # ------------------------------------------------------------------
    # Execution override — maker-specific logic for live mode
    # ------------------------------------------------------------------

    def execute(self, signal: dict, market: dict) -> dict:
        """Override execute to use limit orders in live mode."""
        import config
        import db
        import logging

        log = logging.getLogger(__name__)

        if self._paused:
            return {"success": False, "reason": "bot_paused"}

        self.trading_mode = db.get_bot_mode(self.name)
        mode = self.trading_mode

        # Standard risk checks (inherited logic)
        daily_loss = db.get_bot_daily_loss(self.name, mode)
        max_daily = config.get_max_daily_loss_per_bot()
        if daily_loss >= max_daily:
            self._paused = True
            log.warning(f"[{self.name}] Daily loss limit hit, pausing")
            return {"success": False, "reason": "daily_loss_limit"}

        total_daily = db.get_total_daily_loss(mode)
        if total_daily >= config.get_max_daily_loss_total():
            log.warning(f"[{self.name}] Arena daily loss limit hit")
            return {"success": False, "reason": "arena_loss_limit"}

        max_pos = config.LIVE_MAX_POSITION if mode == "live" else config.PAPER_MAX_POSITION
        amount = min(signal.get("suggested_amount", max_pos * 0.5), max_pos)

        try:
            if mode == "live":
                return self._execute_maker_live(signal, market, amount)
            else:
                return self._execute_paper(signal, market, amount, "simmer", mode)
        except Exception as e:
            log.error(f"[{self.name}] Trade exception: {e}")
            return {"success": False, "reason": str(e)}

    def _execute_maker_live(self, signal: dict, market: dict, amount: float) -> dict:
        """Post limit orders to the Polymarket CLOB."""
        import logging
        import db
        import polymarket_client

        log = logging.getLogger(__name__)

        market_id = market.get("id") or market.get("market_id")
        maker_side = signal.get("maker_side", "yes")
        maker_bid = signal.get("maker_bid")
        maker_ask = signal.get("maker_ask")
        p = self.strategy_params

        # Cancel stale open orders for this market if over the cap
        existing = self._open_orders.get(market_id, [])
        max_open = p.get("max_open_orders", 4)
        while len(existing) >= max_open:
            old_id = existing.pop(0)
            cancel_result = polymarket_client.cancel_order(old_id)
            log.info(f"[{self.name}] Cancelled stale order {old_id}: {cancel_result.get('success')}")

        results = []
        size = p.get("size_shares", 10.0)

        # Post bid (buy YES) if leaning YES or neutral
        if maker_side in ("yes", "both") and maker_bid and market.get("polymarket_token_id"):
            res = polymarket_client.place_limit_order(
                token_id=market["polymarket_token_id"],
                side="buy",
                size=size,
                price=maker_bid,
                order_type="GTC",
            )
            if res.get("success") and res.get("order_id"):
                existing.append(res["order_id"])
                db.log_trade(
                    bot_name=self.name,
                    market_id=market_id,
                    market_question=market.get("question"),
                    side="yes",
                    amount=amount,
                    venue="polymarket",
                    mode="live",
                    confidence=signal["confidence"],
                    reasoning=signal.get("reasoning"),
                    trade_id=res["order_id"],
                    shares_bought=size,
                    trade_features=signal.get("features"),
                )
            results.append(res)

        # Post ask (sell YES / buy NO) if leaning NO or neutral
        if maker_side in ("no", "both") and maker_ask and market.get("polymarket_no_token_id"):
            res = polymarket_client.place_limit_order(
                token_id=market["polymarket_no_token_id"],
                side="buy",
                size=size,
                price=1.0 - maker_ask,  # NO token price = 1 - YES ask
                order_type="GTC",
            )
            if res.get("success") and res.get("order_id"):
                existing.append(res["order_id"])
                db.log_trade(
                    bot_name=self.name,
                    market_id=market_id,
                    market_question=market.get("question"),
                    side="no",
                    amount=amount,
                    venue="polymarket",
                    mode="live",
                    confidence=signal["confidence"],
                    reasoning=signal.get("reasoning"),
                    trade_id=res["order_id"],
                    shares_bought=size,
                    trade_features=signal.get("features"),
                )
            results.append(res)

        self._open_orders[market_id] = existing

        success = any(r.get("success") for r in results)
        log.info(
            f"[{self.name}] LIVE maker orders: {len(results)} posted "
            f"(bid={maker_bid}, ask={maker_ask}) success={success}"
        )
        return {"success": success, "results": results}

    def cancel_all_open_orders(self):
        """Cancel all tracked open orders. Call at shutdown or before evolution."""
        import polymarket_client
        import logging

        log = logging.getLogger(__name__)
        for market_id, order_ids in self._open_orders.items():
            for oid in order_ids:
                res = polymarket_client.cancel_order(oid)
                log.info(f"[{self.name}] Shutdown cancel {oid}: {res.get('success')}")
        self._open_orders.clear()
