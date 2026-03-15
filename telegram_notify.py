"""
telegram_notify.py
Sends trade alerts and status messages to Telegram.
Used by run.py wrapper — does not modify fastloop_trader.py.
"""

import os
import json
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            TELEGRAM_API,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (HTTPError, URLError, Exception):
        return False


def notify_trade(side: str, market: str, amount: float, price: float, momentum: float, dry_run: bool = True, feed: str = "binance"):
    """Alert: a trade was executed (or would be in dry-run)."""
    mode = "🧪 <b>DRY RUN</b>" if dry_run else "✅ <b>LIVE TRADE</b>"
    arrow = "📈" if side.lower() == "yes" else "📉"
    feed_label = {"binance": "Binance", "kraken": "Kraken ⚡", "coingecko": "CoinGecko"}.get(feed, feed)
    msg = (
        f"{arrow} {mode}\n\n"
        f"<b>Market:</b> {market[:60]}\n"
        f"<b>Side:</b> {side.upper()}\n"
        f"<b>Amount:</b> ${amount:.2f}\n"
        f"<b>Price:</b> ${price:.3f}\n"
        f"<b>BTC Momentum:</b> {momentum:+.3f}%\n"
        f"<b>Feed:</b> {feed_label}\n"
    )
    return send(msg)


def notify_error(error: str):
    """Alert: something went wrong."""
    msg = f"⚠️ <b>FastLoop Error</b>\n\n<code>{error[:300]}</code>"
    return send(msg)


def notify_skip(reason: str):
    """Silent skip — only sent if NOTIFY_SKIPS env var is set to '1'."""
    if os.environ.get("NOTIFY_SKIPS") != "1":
        return False
    msg = f"⏸ <b>FastLoop Skip</b>: {reason}"
    return send(msg)


def notify_budget_warning(spent: float, budget: float):
    """Alert when daily budget is >80% consumed."""
    pct = (spent / budget) * 100 if budget > 0 else 0
    msg = (
        f"💰 <b>Budget Warning</b>\n\n"
        f"Spent <b>${spent:.2f}</b> of <b>${budget:.2f}</b> daily budget ({pct:.0f}%)"
    )
    return send(msg)


def notify_startup(mode: str, asset: str, budget: float):
    """Sent once when bot starts a cycle."""
    msg = (
        f"⚡ <b>FastLoop Started</b>\n\n"
        f"Mode: <b>{mode}</b>\n"
        f"Asset: <b>{asset}</b>\n"
        f"Daily Budget: <b>${budget:.2f}</b>"
    )
    return send(msg)
