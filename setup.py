"""
Phase 2: Account Setup & Connection Verification
1. Checks Simmer registration and API key
2. Checks claim status
3. Verifies trading access
4. Places a test trade
5. Discovers active BTC 5-min up/down market
6. Saves results to setup_log.md
"""

import json
import requests
import sys
from pathlib import Path
from datetime import datetime
import config

BASE = config.SIMMER_BASE_URL


def load_api_key():
    try:
        with open(config.SIMMER_API_KEY_PATH) as f:
            return json.load(f).get("api_key")
    except FileNotFoundError:
        return None


def check_agent_status(api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(f"{BASE}/api/sdk/agents/me", headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"Error getting agent status: {resp.status_code} {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"Exception getting agent status: {e}")
        return None


def get_markets(api_key, limit=100):
    """Fetch active markets. Returns a list."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(
        f"{BASE}/api/sdk/markets",
        headers=headers,
        params={"status": "active", "limit": limit},
        timeout=15,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    # API returns a list directly
    if isinstance(data, list):
        return data
    # Or it might be nested
    return data.get("markets", data.get("results", []))


def discover_btc_market(api_key):
    """Find the active BTC 5-min up/down market."""
    all_markets = get_markets(api_key)

    for m in all_markets:
        q = m.get("question", "").lower()
        has_btc = "btc" in q or "bitcoin" in q
        has_updown = "up or down" in q or "up/down" in q
        if has_btc and has_updown:
            mid = m.get("id") or m.get("market_id")
            print(f"Found BTC 5-min market: {mid}")
            print(f"   Question: {m.get('question')}")
            print(f"   Price: {m.get('current_price', 'N/A')}")
            return m

    # Fallback: show what BTC markets exist
    btc_markets = [m for m in all_markets if "bitcoin" in m.get("question", "").lower()]
    if btc_markets:
        print(f"No BTC up/down market found. Found {len(btc_markets)} BTC markets:")
        for bm in btc_markets[:5]:
            print(f"   - {bm.get('question', '')[:80]}")
    else:
        print("No BTC markets found at all.")
    return None


def place_test_trade(api_key):
    """Place a $1 SIM test trade to verify the pipeline."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    all_markets = get_markets(api_key, limit=10)
    if not all_markets:
        return {"success": False, "error": "No active markets"}

    test_market = all_markets[0]
    market_id = test_market.get("id") or test_market.get("market_id")
    question = test_market.get("question", "Unknown")

    print(f"\n   Test market: {question}")
    print(f"   Market ID: {market_id}")

    payload = {
        "market_id": market_id,
        "side": "yes",
        "amount": 1.0,
        "venue": "simmer",
        "source": "arena:setup-test",
        "reasoning": "Setup verification test trade",
    }

    print(f"   Placing: $1 $SIM on YES...")
    resp = requests.post(f"{BASE}/api/sdk/trade", headers=headers, json=payload, timeout=15)

    if resp.status_code in (200, 201):
        result = resp.json()
        print(f"   Trade successful!")
        return {"success": True, "trade_id": result.get("trade_id", result.get("id")), "market": question, "result": result}
    else:
        print(f"   Trade failed: {resp.status_code}")
        print(f"   Response: {resp.text[:300]}")
        return {"success": False, "error": f"{resp.status_code}: {resp.text[:200]}"}


def save_setup_log(agent_info, market, test_trade):
    log_path = Path(__file__).parent / "setup_log.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    market_id = (market.get("id") or market.get("market_id", "Not found")) if market else "Not found"
    market_q = market.get("question", "N/A") if market else "N/A"
    balance = agent_info.get("balance", "N/A")
    real_trading = agent_info.get("real_trading_enabled", False)

    log_content = f"""# Polymarket Bot Arena - Setup Log

**Generated:** {timestamp}

## Agent Status

- **Agent ID:** {agent_info.get('agent_id', 'N/A')}
- **Agent Name:** {agent_info.get('name', 'N/A')}
- **Status:** {agent_info.get('status', 'N/A')}
- **Claimed:** {agent_info.get('claimed', False)}
- **Real Trading Enabled:** {real_trading}
- **Balance ($SIM):** {balance}

## Target Market: BTC 5-Min Up/Down

- **Market ID:** {market_id}
- **Question:** {market_q}

## Test Trade

- **Success:** {test_trade.get('success', False)}
- **Trade ID:** {test_trade.get('trade_id', 'N/A')}
- **Market:** {test_trade.get('market', 'N/A')}
- **Error:** {test_trade.get('error', 'None')}

## Next Steps

"""
    if test_trade.get("success"):
        log_content += "Setup complete. Ready to run the arena in paper mode.\n"
    else:
        log_content += f"Test trade failed. Fix before proceeding.\nError: {test_trade.get('error', 'Unknown')}\n"

    log_path.write_text(log_content)
    print(f"\n   Setup log saved to: {log_path}")


def main():
    print("=" * 60)
    print("Polymarket Bot Arena - Setup")
    print("=" * 60)

    # 1. API key
    print("\n1. Checking Simmer API key...")
    api_key = load_api_key()
    if not api_key:
        print(f"   No API key at {config.SIMMER_API_KEY_PATH}")
        sys.exit(1)
    print(f"   API key loaded")

    # 2. Agent status
    print("\n2. Checking agent status...")
    agent_info = check_agent_status(api_key)
    if not agent_info:
        print("   Could not get agent status.")
        sys.exit(1)

    print(f"   Agent: {agent_info.get('name')} ({agent_info.get('agent_id')})")
    print(f"   Status: {agent_info.get('status')}")
    print(f"   Claimed: {agent_info.get('claimed')}")
    print(f"   Real trading: {agent_info.get('real_trading_enabled')}")
    print(f"   Balance: ${agent_info.get('balance', 0):,.2f} $SIM")

    # 3. Discover BTC market
    print("\n3. Discovering BTC 5-min up/down market...")
    market = discover_btc_market(api_key)

    # 4. Test trade
    print("\n4. Placing test trade...")
    test_trade = place_test_trade(api_key)

    # 5. Save log
    print("\n5. Saving setup log...")
    save_setup_log(agent_info, market, test_trade)

    print("\n" + "=" * 60)
    if test_trade.get("success"):
        print("SETUP COMPLETE — ready to run: python arena.py")
    else:
        print("SETUP INCOMPLETE — fix test trade first")
    print("=" * 60)

    return test_trade.get("success", False)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
