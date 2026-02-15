"""Set up multiple Simmer accounts for independent bot trading.

Each bot gets its own Simmer agent so they can trade independently on the
same markets, creating real competition for learning and evolution.

Usage:
    python3 setup_multi_account.py

You'll need 4 Simmer API keys (one per bot slot):
    1. Go to https://simmer.markets
    2. Dashboard -> SDK tab -> Generate API key
    3. Repeat for 3 more accounts (use different email/login for each)
    4. Paste each key when prompted
"""

import json
import sys
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent))
import config

SLOTS = ["slot_0", "slot_1", "slot_2", "slot_3"]
BOT_NAMES = ["momentum-v1", "meanrev-v1", "sentiment-v1", "hybrid-v1"]


def verify_key(api_key):
    """Verify a Simmer API key and return agent info."""
    try:
        resp = requests.get(
            f"{config.SIMMER_BASE_URL}/api/sdk/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def main():
    print("=" * 60)
    print("Polymarket Bot Arena - Multi-Account Setup")
    print("=" * 60)
    print()
    print("Each bot needs its own Simmer account for independent trading.")
    print("Create 4 agents at https://simmer.markets (Dashboard -> SDK tab)")
    print()

    keys_path = config.SIMMER_BOT_KEYS_PATH
    existing = {}
    try:
        with open(keys_path) as f:
            existing = json.load(f)
        print(f"Found existing bot_keys.json with {len(existing)} slots")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Check if default key is already set
    default_key = None
    try:
        with open(config.SIMMER_API_KEY_PATH) as f:
            default_key = json.load(f).get("api_key")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    bot_keys = {}
    for i, slot in enumerate(SLOTS):
        bot_name = BOT_NAMES[i] if i < len(BOT_NAMES) else f"bot-{i}"
        print(f"\n--- {slot} ({bot_name}) ---")

        # Check if already configured
        if slot in existing:
            info = verify_key(existing[slot])
            if info:
                print(f"  Already configured: {info.get('name')} (${info.get('balance', 0):,.0f} $SIM)")
                reuse = input("  Keep this key? [Y/n]: ").strip().lower()
                if reuse != "n":
                    bot_keys[slot] = existing[slot]
                    continue

        # Use default key for slot_0 if available
        if i == 0 and default_key and slot not in existing:
            info = verify_key(default_key)
            if info:
                print(f"  Using existing default key: {info.get('name')} (${info.get('balance', 0):,.0f} $SIM)")
                bot_keys[slot] = default_key
                continue

        # Prompt for new key
        while True:
            key = input(f"  Paste API key for {slot} (or 'skip'): ").strip()
            if key.lower() == "skip":
                if default_key:
                    print(f"  Skipped — will use default key (shared)")
                    bot_keys[slot] = default_key
                break

            info = verify_key(key)
            if info:
                print(f"  Verified: {info.get('name')} (${info.get('balance', 0):,.0f} $SIM)")
                bot_keys[slot] = key
                break
            else:
                print("  Invalid key — try again")

    # Save
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    with open(keys_path, "w") as f:
        json.dump(bot_keys, f, indent=2)

    # Check how many unique keys we have
    unique_keys = len(set(bot_keys.values()))
    print(f"\n{'=' * 60}")
    print(f"Saved {len(bot_keys)} slots with {unique_keys} unique Simmer accounts")
    print(f"Keys saved to: {keys_path}")

    if unique_keys >= 4:
        print("\nFull independence! Each bot trades on its own account.")
    elif unique_keys > 1:
        print(f"\nPartial independence: {unique_keys} unique accounts. Bots sharing a key will use voting.")
    else:
        print("\nAll bots share one account — voting mode will be used.")

    print(f"\nRun the arena: python3 arena.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
