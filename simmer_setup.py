#!/usr/bin/env python3
"""
simmer_setup.py
Run once to register Simmer's native clawdbot Telegram alerts.
This wires Simmer's backend to push trade/risk alerts directly to your Telegram.

Usage:
  python simmer_setup.py
"""

import os
import sys
import json
from urllib.request import urlopen, Request
from urllib.error import HTTPError

API_KEY      = os.environ.get("SIMMER_API_KEY", "")
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE         = "https://api.simmer.markets"

def api(method, path, data=None):
    url = BASE + path
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method,
                  headers={"Authorization": f"Bearer {API_KEY}",
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except HTTPError as e:
        return json.loads(e.read()), e.code

def check_health():
    req = Request(f"{BASE}/api/sdk/health")
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

if not API_KEY:
    print("❌ SIMMER_API_KEY not set"); sys.exit(1)
if not CHAT_ID:
    print("❌ TELEGRAM_CHAT_ID not set"); sys.exit(1)

print("\n🔧 Simmer Setup\n" + "=" * 50)

# 1. Health check
h = check_health()
print(f"API health: {h.get('status', 'unknown')} (v{h.get('version','?')})")

# 2. Check agent status
data, code = api("GET", "/api/sdk/agents/me")
if code != 200:
    print(f"❌ Agent check failed ({code}): {data}"); sys.exit(1)

print(f"Agent:      {data.get('name')} [{data.get('status')}]")
print(f"Real trade: {data.get('real_trading_enabled')}")
print(f"Balance:    {data.get('balance')} $SIM")

# 3. Register Simmer native Telegram alerts (clawdbot)
print("\n📱 Registering Simmer native Telegram alerts...")
settings_payload = {
    "clawdbot_chat_id":   CHAT_ID,
    "clawdbot_channel":   "telegram",
}
result, code = api("POST", "/api/sdk/settings", settings_payload)
if code in (200, 201):
    print(f"✅ Simmer Telegram alerts registered → {CHAT_ID}")
else:
    print(f"⚠️  Settings update returned {code}: {result}")

# 4. Confirm wallet + trading settings
print("\n⚙️  Checking trading settings...")
settings, code = api("GET", "/api/sdk/settings")
if code == 200:
    print(f"  Wallet type:     {settings.get('wallet_type', 'unknown')}")
    print(f"  Trading paused:  {settings.get('trading_paused', False)}")
    print(f"  Stop-loss:       {settings.get('default_stop_loss_pct', 'not set')}")
    print(f"  Daily cap:       {settings.get('max_trades_per_day', 'not set')}")
    print(f"  Max position:    ${settings.get('max_position_usd', 'not set')}")
else:
    print(f"  ⚠️  Could not fetch settings ({code})")

# 5. Test troubleshoot endpoint (no auth needed, verifies network)
print("\n🌐 Testing Simmer API reachability from this machine...")
test_req = Request(
    f"{BASE}/api/sdk/troubleshoot",
    data=json.dumps({"error_text": "test"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"
)
try:
    with urlopen(test_req, timeout=10) as r:
        print(f"  ✅ Simmer API reachable (status {r.status})")
except Exception as e:
    print(f"  ❌ Simmer API unreachable: {e}")
    print(f"  → Railway will use Gamma API fallback (markets still discoverable)")

print("\n" + "=" * 50)
print("✅ Setup complete. Simmer will now push alerts to your Telegram.")
print("   Test it: place a dry-run trade and watch for a message.")
print("=" * 50 + "\n")
