#!/usr/bin/env python3
"""
telegram_commands.py
Polls Telegram for incoming commands and replies instantly.
Called once at the start of each cron cycle — checks for pending commands,
replies, then returns. No always-on server needed.

Supported commands:
  /status  — bot alive, last cycle time, Simmer API reachability
  /config  — current strategy settings
  /last    — last cycle result (trade/skip/error)
  /budget  — today's spend vs daily limit
"""

import os
import json
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE    = f"https://api.telegram.org/bot{TOKEN}"

# State file — shared with run.py to persist last cycle info
STATE_FILE = "/tmp/fastloop_state.json"
OFFSET_FILE = "/tmp/tg_offset.json"


def _http(url, data=None, timeout=8):
    try:
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body,
                      headers={"Content-Type": "application/json"} if body else {},
                      method="POST" if body else "GET")
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _send(chat_id, text):
    _http(f"{BASE}/sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    })


def _load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def _save_offset(offset):
    try:
        with open(OFFSET_FILE, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception:
        pass


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cycle_state(result: str, why: str, trades: int, amount: float):
    """Called by run.py after each cycle to persist state for /last and /budget."""
    try:
        state = _load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Reset daily spend if new day
        if state.get("date") != today:
            state["daily_spent"] = 0.0
            state["daily_trades"] = 0
            state["date"] = today
        if trades > 0:
            state["daily_spent"] = state.get("daily_spent", 0.0) + amount
            state["daily_trades"] = state.get("daily_trades", 0) + trades
        state["last_result"] = result
        state["last_why"] = why
        state["last_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _handle_command(cmd, chat_id):
    """Generate reply for a given command."""
    state = _load_state()

    if cmd == "/status":
        last_ts  = state.get("last_ts", "never")
        last_res = state.get("last_result", "unknown")
        # Quick Simmer health check
        try:
            r = _http("https://api.simmer.markets/api/sdk/health", timeout=5)
            simmer = f"✅ v{r.get('version','?')}" if r.get("status") == "ok" else "⚠️ unreachable"
        except Exception:
            simmer = "⚠️ unreachable"
        mode = "🔴 LIVE" if os.environ.get("LIVE_TRADING") == "1" else "🧪 DRY RUN"
        reply = (
            f"⚡ <b>FastLoop Status</b>\n\n"
            f"Mode:        {mode}\n"
            f"Simmer API:  {simmer}\n"
            f"Last cycle:  {last_ts}\n"
            f"Last result: {last_res}\n"
            f"Cron:        every 5 min 24/7"
        )

    elif cmd == "/config":
        reply = (
            f"⚙️ <b>FastLoop Config</b>\n\n"
            f"Asset:       {os.environ.get('SIMMER_SPRINT_ASSET','BTC')}\n"
            f"Window:      {os.environ.get('SIMMER_SPRINT_WINDOW','5m')}\n"
            f"Momentum:    {os.environ.get('SIMMER_FASTLOOP_MOMENTUM_THRESHOLD','0.2')}%\n"
            f"Entry edge:  {os.environ.get('SIMMER_FASTLOOP_ENTRY_THRESHOLD','0.03')}\n"
            f"Max pos:     ${os.environ.get('SIMMER_FASTLOOP_MAX_POSITION_USD','5')}\n"
            f"Lookback:    {os.environ.get('SIMMER_FASTLOOP_LOOKBACK_MINUTES','5')} min\n"
            f"Daily budget:${os.environ.get('DAILY_BUDGET_USD','20')}"
        )

    elif cmd == "/last":
        last_ts  = state.get("last_ts", "no cycles yet")
        last_res = state.get("last_result", "—")
        last_why = state.get("last_why", "—")
        reply = (
            f"🕐 <b>Last Cycle</b>\n\n"
            f"Time:   {last_ts}\n"
            f"Result: {last_res}\n"
            f"Detail: {last_why}"
        )

    elif cmd == "/budget":
        daily_limit  = float(os.environ.get("DAILY_BUDGET_USD", "20"))
        daily_spent  = state.get("daily_spent", 0.0)
        daily_trades = state.get("daily_trades", 0)
        remaining    = daily_limit - daily_spent
        pct          = (daily_spent / daily_limit * 100) if daily_limit > 0 else 0
        bar_filled   = int(pct / 10)
        bar          = "█" * bar_filled + "░" * (10 - bar_filled)
        reply = (
            f"💰 <b>Today's Budget</b>\n\n"
            f"Spent:     ${daily_spent:.2f} / ${daily_limit:.2f}\n"
            f"Remaining: ${remaining:.2f}\n"
            f"Trades:    {daily_trades}\n"
            f"[{bar}] {pct:.0f}%\n"
            f"Resets at UTC midnight"
        )

    else:
        reply = (
            f"⚡ <b>FastLoop Commands</b>\n\n"
            f"/status — bot alive + Simmer API\n"
            f"/config — current strategy settings\n"
            f"/last   — last cycle result\n"
            f"/budget — today's spend vs limit"
        )

    _send(chat_id, reply)


def poll_and_respond():
    """
    Check Telegram for pending commands and reply.
    Runs once per cron cycle — fast, non-blocking.
    """
    if not TOKEN or not CHAT_ID:
        return

    offset = _load_offset()
    result = _http(f"{BASE}/getUpdates?offset={offset}&timeout=1&limit=10")

    updates = result.get("result", [])
    if not updates:
        return

    for update in updates:
        offset = max(offset, update.get("update_id", 0) + 1)
        msg = update.get("message", {})
        if not msg:
            continue
        text    = msg.get("text", "").strip().lower().split()[0] if msg.get("text") else ""
        chat_id = msg.get("chat", {}).get("id")
        # Only respond to your own chat for security
        if str(chat_id) != str(CHAT_ID):
            continue
        if text.startswith("/"):
            _handle_command(text, chat_id)

    _save_offset(offset)
