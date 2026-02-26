"""Direct Polymarket CLOB client for live trading."""

import json
import logging
import math
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_order_utils.model import POLY_PROXY

import config

logger = logging.getLogger(__name__)

_client = None


def _load_creds():
    with open(config.POLYMARKET_KEY_PATH) as f:
        return json.load(f)


def get_client() -> ClobClient:
    """Get or create the CLOB client singleton."""
    global _client
    if _client is None:
        creds = _load_creds()
        pk = creds["private_key"]
        funder = creds.get("wallet_address")  # proxy wallet (0xcdc7609) holding USDC
        _client = ClobClient(
            host=config.POLYMARKET_HOST,
            key=pk,
            chain_id=config.POLYMARKET_CHAIN_ID,
            funder=funder,
            signature_type=POLY_PROXY,  # type 1: Polymarket proxy wallet
        )
        # Derive API credentials from the wallet
        _client.set_api_creds(_client.create_or_derive_api_creds())
        logger.info(f"Polymarket CLOB client initialized (funder={funder})")
    return _client


def get_usdc_balance() -> float:
    """Get USDC balance from Polymarket CLOB (funder wallet collateral)."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = get_client()
        result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = result.get("balance", "0")
        return int(raw) / 1e6  # USDC has 6 decimals
    except Exception as e:
        logger.error(f"USDC balance check failed: {e}")
        return None


def get_market_info(token_id: str) -> dict:
    """Get current market/book info for a token."""
    try:
        client = get_client()
        book = client.get_order_book(token_id)
        return {
            "bids": book.bids if book.bids else [],
            "asks": book.asks if book.asks else [],
            "best_bid": float(book.bids[0].price) if book.bids else 0,
            "best_ask": float(book.asks[0].price) if book.asks else 1,
        }
    except Exception as e:
        logger.error(f"Market info error: {e}")
        return {}


def place_market_order(token_id: str, side: str, amount: float) -> dict:
    """Place a market buy order on Polymarket.

    Args:
        token_id: The YES or NO token ID from the market
        side: "yes" or "no"
        amount: USDC amount to spend
    """
    try:
        client = get_client()

        # Get the best price from the order book
        book = client.get_order_book(token_id)

        if side.lower() == "yes":
            # Buying YES tokens — take the best ask
            if not book.asks:
                return {"success": False, "error": "No asks in order book"}
            price = float(book.asks[0].price)
        else:
            # Buying NO tokens — the NO token_id should be used
            if not book.asks:
                return {"success": False, "error": "No asks in order book"}
            price = float(book.asks[0].price)

        # Build and sign the order
        order_args = OrderArgs(
            price=price,
            size=math.ceil(amount / price * 100) / 100,  # Round UP so cost >= amount (avoids $0.9999 rejection)
            side=BUY,
            token_id=token_id,
        )

        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, OrderType.GTC)

        logger.info(f"Polymarket order placed: {side} ${amount} at {price}")
        return {
            "success": True,
            "order_id": result.get("orderID"),
            "price": price,
            "size": order_args.size,
            "result": result,
        }

    except Exception as e:
        logger.error(f"Polymarket order failed: {e}")
        return {"success": False, "error": str(e)}


def place_limit_order(
    token_id: str,
    side: str,
    size: float,
    price: float,
    *,
    order_type: str = "GTC",
    neg_risk: bool = False,
) -> dict:
    """Place a maker limit order on the Polymarket CLOB.

    Unlike place_market_order (which takes the best ask immediately), this
    posts a resting limit order to the book.  The order sits passively and
    earns the maker rebate when matched.

    Args:
        token_id: YES or NO token ID for the market.
        side:     "buy" to post a bid, "sell" to post an ask.
        size:     Number of shares (e.g. 10.0 → 10 contracts).
        price:    Limit price in USDC cents per share (0.0–1.0).
        order_type: "GTC" (default), "GTD", or "FOK".
        neg_risk: Set True for neg-risk markets (e.g. multi-outcome).

    Returns:
        {
            "success": bool,
            "order_id": str | None,
            "price": float,
            "size": float,
            "status": str,          # "live" | "matched" | "delayed" | …
            "result": dict,         # raw CLOB response
        }
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    try:
        client = get_client()

        clob_side = BUY if side.lower() == "buy" else SELL
        clob_order_type = {
            "GTC": OrderType.GTC,
            "GTD": OrderType.GTD,
            "FOK": OrderType.FOK,
        }.get(order_type.upper(), OrderType.GTC)

        order_args = OrderArgs(
            price=round(price, 4),
            size=round(size, 2),
            side=clob_side,
            token_id=token_id,
        )

        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, clob_order_type)

        logger.info(
            f"Polymarket limit order posted: {side} {size} @ {price:.4f} "
            f"({order_type}) order_id={result.get('orderID')}"
        )
        return {
            "success": True,
            "order_id": result.get("orderID"),
            "price": price,
            "size": size,
            "status": result.get("status", "unknown"),
            "result": result,
        }

    except Exception as e:
        logger.error(f"Polymarket limit order failed: {e}")
        return {"success": False, "error": str(e)}


def cancel_order(order_id: str) -> dict:
    """Cancel a resting limit order by order ID.

    Returns:
        {"success": bool, "result": dict | None, "error": str | None}
    """
    try:
        client = get_client()
        result = client.cancel(order_id=order_id)
        logger.info(f"Polymarket order cancelled: {order_id}")
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Polymarket cancel failed for {order_id}: {e}")
        return {"success": False, "error": str(e)}


def get_open_orders(token_id: str | None = None) -> list[dict]:
    """Fetch all open (resting) orders, optionally filtered by token_id.

    Returns a list of order dicts from the CLOB.
    """
    try:
        client = get_client()
        params = {}
        if token_id:
            params["token_id"] = token_id
        orders = client.get_orders(**params) if params else client.get_orders()
        return orders if isinstance(orders, list) else []
    except Exception as e:
        logger.error(f"get_open_orders failed: {e}")
        return []


def compute_maker_quotes(
    token_id: str,
    *,
    spread_ticks: int = 2,
    tick_size: float = 0.01,
    size: float = 10.0,
) -> dict:
    """Compute symmetric bid/ask quotes around the current mid-price.

    Used by maker bots to decide where to post limit orders.

    Args:
        token_id:     Token to quote.
        spread_ticks: Half-spread in ticks (default 2 → ±2¢).
        tick_size:    Minimum price increment (Polymarket = $0.01).
        size:         Shares to post on each side.

    Returns:
        {
            "mid":      float,
            "bid":      float,   # price to post a buy limit
            "ask":      float,   # price to post a sell limit
            "size":     float,
            "valid":    bool,    # False if no book data available
        }
    """
    info = get_market_info(token_id)
    best_bid = info.get("best_bid", 0.0)
    best_ask = info.get("best_ask", 1.0)

    if best_bid <= 0 or best_ask >= 1 or best_ask <= best_bid:
        return {"mid": 0.5, "bid": 0.0, "ask": 1.0, "size": size, "valid": False}

    mid = round((best_bid + best_ask) / 2, 4)
    half = spread_ticks * tick_size
    bid = round(max(0.01, mid - half), 4)
    ask = round(min(0.99, mid + half), 4)

    return {"mid": mid, "bid": bid, "ask": ask, "size": size, "valid": True}


def verify_connection() -> dict:
    """Verify the Polymarket CLOB connection works."""
    try:
        client = get_client()
        # Try to fetch server time as a connectivity check
        ok = client.get_ok()
        return {"connected": True, "status": ok}
    except Exception as e:
        return {"connected": False, "error": str(e)}
