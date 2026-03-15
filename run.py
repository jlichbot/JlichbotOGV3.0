#!/usr/bin/env python3
"""
run.py — Railway entrypoint for FastLoop Trader
- Polls Telegram for /status /config /last /budget commands each cycle
- Runs fastloop_trader.py with full diagnostic output
- Sends alerts via Telegram on trades/errors
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

try:
    import price_fallback  # noqa
except Exception as e:
    print(f"⚠️  price_fallback patch warning: {e}", flush=True)

from telegram_notify import notify_trade, notify_error, notify_skip, notify_budget_warning
from telegram_commands import poll_and_respond, save_cycle_state

# ── Config ─────────────────────────────────────────────────────────────────────
LIVE_TRADING  = os.environ.get("LIVE_TRADING", "0") == "1"
SMART_SIZING  = os.environ.get("SMART_SIZING", "0") == "1"
DAILY_BUDGET  = float(os.environ.get("DAILY_BUDGET_USD", "20"))
ASSET         = os.environ.get("SIMMER_SPRINT_ASSET", "BTC")
WINDOW        = os.environ.get("SIMMER_SPRINT_WINDOW", "5m")
ENTRY_THRESH  = os.environ.get("SIMMER_FASTLOOP_ENTRY_THRESHOLD", "0.03")
MOMENTUM_MIN  = os.environ.get("SIMMER_FASTLOOP_MOMENTUM_THRESHOLD", "0.2")
MAX_POS       = os.environ.get("SIMMER_FASTLOOP_MAX_POSITION_USD", "5")
SIMMER_KEY    = os.environ.get("SIMMER_API_KEY", "")

os.environ["AUTOMATON_MANAGED"] = "1"

# ── Poll Telegram for commands FIRST (before anything else) ────────────────────
try:
    poll_and_respond()
except Exception as e:
    print(f"⚠️  Telegram command poll error: {e}", flush=True)

# ── Simmer API health check ────────────────────────────────────────────────────
def check_simmer_reachable():
    try:
        req = Request("https://api.simmer.markets/api/sdk/health")
        with urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            return True, data.get("version", "?")
    except Exception as e:
        return False, str(e)

# ── Cycle header ───────────────────────────────────────────────────────────────
ts         = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
mode_label = "LIVE" if LIVE_TRADING else "DRY RUN"

print("", flush=True)
print("=" * 60, flush=True)
print(f"  FASTLOOP  |  {ts}  |  {mode_label}", flush=True)
print("=" * 60, flush=True)
print(f"  Asset: {ASSET}  Window: {WINDOW}  Budget: ${DAILY_BUDGET}", flush=True)
print(f"  Entry: {ENTRY_THRESH}  Momentum: {MOMENTUM_MIN}%  Max pos: ${MAX_POS}", flush=True)
print("-" * 60, flush=True)

reachable, version = check_simmer_reachable()
if reachable:
    print(f"  Simmer API: ✅ reachable (v{version})", flush=True)
else:
    print(f"  Simmer API: ⚠️  unreachable — using Gamma fallback", flush=True)
print("-" * 60, flush=True)

# ── Build subprocess command ───────────────────────────────────────────────────
cmd = [sys.executable, "fastloop_trader.py"]
if LIVE_TRADING:
    cmd.append("--live")
if SMART_SIZING:
    cmd.append("--smart-sizing")

_here = os.path.dirname(os.path.abspath(__file__))
_env  = os.environ.copy()
_env["PYTHONPATH"]    = _here + (":" + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
_env["PYTHONSTARTUP"] = os.path.join(_here, "price_fallback.py")

# ── Run trader ────────────────────────────────────────────────────────────────
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=_env)
except subprocess.TimeoutExpired:
    msg = "FastLoop timed out after 90s"
    print(f"RESULT: TIMEOUT — {msg}", flush=True)
    notify_error(msg)
    save_cycle_state("TIMEOUT", msg, 0, 0)
    sys.exit(1)

stdout = result.stdout or ""
stderr = result.stderr or ""

print(stdout, flush=True)
if stderr.strip():
    print(f"[STDERR]\n{stderr}", flush=True)

# ── Parse automaton JSON ───────────────────────────────────────────────────────
automaton_data = None
for line in stdout.splitlines():
    if line.strip().startswith('{"automaton"'):
        try:
            automaton_data = json.loads(line.strip()).get("automaton", {})
        except json.JSONDecodeError:
            pass

# ── Extract context ────────────────────────────────────────────────────────────
lines        = stdout.splitlines()
momentum_val = 0.0
price_val    = 0.0
side_val     = "YES"
price_source = "binance"
market_name  = ""
market_id    = ""

for l in lines:
    if "Momentum:" in l:
        try: momentum_val = float(l.split("Momentum:")[1].strip().split("%")[0].replace("+",""))
        except Exception: pass
    if "YES price:" in l or "YES $" in l:
        try: price_val = float(l.split("$")[1].strip().split()[0])
        except Exception: pass
    if "Signal: YES" in l: side_val = "YES"
    elif "Signal: NO" in l: side_val = "NO"
    if "Price source:" in l:
        try: price_source = l.split("Price source:")[1].strip().split()[0]
        except Exception: pass
    if "Selected:" in l:
        market_name = l.replace("Selected:", "").replace("🎯","").strip()
    if "Market ID:" in l or "Market ready:" in l:
        try: market_id = l.split(":")[-1].strip().split("...")[0]
        except Exception: pass

# ── Results ────────────────────────────────────────────────────────────────────
print("-" * 60, flush=True)

if result.returncode != 0:
    err = (stderr or stdout)[:300]
    print(f"RESULT: CRASH  exit={result.returncode}", flush=True)
    notify_error(f"Trader crashed (exit {result.returncode})\n{err}")
    save_cycle_state("CRASH", f"exit={result.returncode}", 0, 0)
    print("=" * 60, flush=True)
    sys.exit(result.returncode)

if automaton_data:
    trades_executed  = automaton_data.get("trades_executed", 0)
    trades_attempted = automaton_data.get("trades_attempted", 0)
    amount_usd       = automaton_data.get("amount_usd", 0.0)
    skip_reason      = automaton_data.get("skip_reason", "")
    signals          = automaton_data.get("signals", 0)
    exec_errors      = automaton_data.get("execution_errors", [])

    if trades_executed > 0:
        tag = "PAPER" if not LIVE_TRADING else "LIVE"
        print(f"RESULT: TRADE EXECUTED [{tag}]", flush=True)
        print(f"  Side: {side_val}  Amount: ${amount_usd:.2f}  Price: ${price_val:.3f}", flush=True)
        print(f"  Market: {market_name[:55]}", flush=True)
        print(f"  Momentum: {momentum_val:+.3f}%  Feed: {price_source}", flush=True)
        notify_trade(side=side_val, market=market_name or "BTC Fast Market",
                     amount=amount_usd, price=price_val, momentum=momentum_val,
                     dry_run=not LIVE_TRADING)
        save_cycle_state(f"TRADE {tag}", f"{side_val} ${amount_usd:.2f} @ ${price_val:.3f}", trades_executed, amount_usd)
        print(f"  Telegram: trade alert sent", flush=True)
        if DAILY_BUDGET > 0 and amount_usd / DAILY_BUDGET > 0.8:
            notify_budget_warning(amount_usd, DAILY_BUDGET)

    elif trades_attempted > 0 and exec_errors:
        print(f"RESULT: TRADE FAILED", flush=True)
        for e in exec_errors:
            print(f"  Error: {e}", flush=True)
        notify_error("Trade attempted but failed:\n" + "\n".join(exec_errors))
        save_cycle_state("TRADE FAILED", "\n".join(exec_errors), 0, 0)

    else:
        low = stdout.lower()
        if "no active fast markets" in low or "found 0" in low:
            why = "NO MARKETS — 0 live BTC markets found"
            hint = "Polymarket may have a gap in the schedule"
        elif "no fast markets with" in low or "no tradeable markets" in low:
            why = "NO MARKETS — all found markets not yet live"
            hint = "Waiting for next 5m window to open"
        elif "momentum" in low and "< minimum" in low:
            actual = next((l.strip() for l in lines if "Momentum" in l and "minimum" in l), "")
            why = f"WEAK SIGNAL — {actual or 'momentum < ' + MOMENTUM_MIN + '%'}"
            hint = f"Lower SIMMER_FASTLOOP_MOMENTUM_THRESHOLD below {MOMENTUM_MIN}"
        elif "divergence" in low and "minimum" in low:
            why = f"WEAK SIGNAL — divergence < {ENTRY_THRESH}"
            hint = "Market priced fairly, no edge"
        elif "already holding" in low:
            why = "SKIPPED — already holding this market"
            hint = "Dedup protection working correctly"
        elif "wide spread" in low:
            why = "SKIPPED — order book spread too wide"
            hint = "Illiquid market, protecting against bad fills"
        elif "fees eat" in low:
            why = "SKIPPED — edge too small after 10% fee"
            hint = "Need stronger signal to overcome fee drag"
        elif "daily budget" in low and "exhausted" in low:
            why = f"BUDGET — daily limit ${DAILY_BUDGET} reached"
            hint = "Resets at UTC midnight"
        elif "all price sources failed" in low or "failed to fetch price" in low:
            why = "PRICE FEED ERROR — all 5 sources failed"
            notify_error("All price feeds failed (Binance/OKX/Kraken/Bybit)")
            hint = "Network issue on Railway"
        elif "clob price unavailable" in low:
            why = "PRICE FEED ERROR — Polymarket CLOB unavailable"
            hint = "Polymarket API may be down"
        else:
            why = f"NO SIGNAL — {skip_reason or 'no qualifying conditions'}"
            hint = ""

        print(f"RESULT: NO TRADE", flush=True)
        print(f"  Why:  {why}", flush=True)
        if hint:
            print(f"  Hint: {hint}", flush=True)

        save_cycle_state("NO TRADE", why, 0, 0)
        if os.environ.get("NOTIFY_SKIPS") == "1":
            notify_skip(why)

else:
    low = stdout.lower()
    if not stdout.strip():
        msg = "EMPTY OUTPUT — check SIMMER_API_KEY"
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
        save_cycle_state("ERROR", msg, 0, 0)
    elif "api key" in low or "authorization" in low:
        msg = "AUTH ERROR — SIMMER_API_KEY rejected"
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
        save_cycle_state("ERROR", msg, 0, 0)
    else:
        why = "no structured report"
        print(f"RESULT: UNKNOWN — {why}", flush=True)
        save_cycle_state("UNKNOWN", why, 0, 0)

print("=" * 60, flush=True)
print(f"  Done  |  {ts}", flush=True)
print("", flush=True)
sys.exit(0)
