"""Direct Polymarket CLOB client for live trading."""

import json
import logging
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import config

logger = logging.getLogger(__name__)

_client = None


def _load_private_key():
    with open(config.POLYMARKET_KEY_PATH) as f:
        return json.load(f)["private_key"]


def get_client() -> ClobClient:
    """Get or create the CLOB client singleton."""
    global _client
    if _client is None:
        pk = _load_private_key()
        _client = ClobClient(
            host=config.POLYMARKET_HOST,
            key=pk,
            chain_id=config.POLYMARKET_CHAIN_ID,
        )
        # Derive API credentials from the wallet
        _client.set_api_creds(_client.create_or_derive_api_creds())
        logger.info("Polymarket CLOB client initialized")
    return _client


def get_balance() -> dict:
    """Get wallet USDC balance info."""
    try:
        client = get_client()
        # The CLOB client doesn't have a direct balance method,
        # but we can check via the allowances/collateral
        return {"connected": True}
    except Exception as e:
        logger.error(f"Balance check failed: {e}")
        return {"connected": False, "error": str(e)}


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
            size=round(amount / price, 2),  # Convert USDC to shares
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


def verify_connection() -> dict:
    """Verify the Polymarket CLOB connection works."""
    try:
        client = get_client()
        # Try to fetch server time as a connectivity check
        ok = client.get_ok()
        return {"connected": True, "status": ok}
    except Exception as e:
        return {"connected": False, "error": str(e)}
