#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Agents can customize signal source.

Usage:
    python fast_trader.py              # Dry run (show opportunities, no trades)
    python fast_trader.py --live       # Execute real trades
    python fast_trader.py --positions  # Show current fast market positions
    python fast_trader.py --quiet      # Only output on trades/errors

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import json
import math
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_FASTLOOP_ENTRY_THRESHOLD", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_FASTLOOP_MOMENTUM_THRESHOLD", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "SIMMER_FASTLOOP_MAX_POSITION_USD", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str,
                      "help": "Price feed source (binance)"},
    "lookback_minutes": {"default": 5, "env": "SIMMER_FASTLOOP_LOOKBACK_MINUTES", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 0, "env": "SIMMER_SPRINT_MIN_TIME", "type": int,
                           "help": "Skip fast_markets with less than this many seconds remaining (0 = auto: 10%% of window)"},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "SIMMER_FASTLOOP_VOL_CONFIDENCE_MIN", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
    "daily_budget": {"default": 10.0, "env": "SIMMER_FASTLOOP_DAILY_BUDGET_USD", "type": float,
                     "help": "Max total spend per UTC day"},
    "use_fair_value": {"default": False, "env": "SIMMER_FASTLOOP_FAIR_VALUE", "type": bool,
                       "help": "Use N(d) fair-value model instead of raw momentum signal"},
    "fair_value_min_edge": {"default": 0.05, "env": "SIMMER_FASTLOOP_FV_MIN_EDGE", "type": float,
                            "help": "Minimum |market_price - fair_value| edge required to trade"},
    "btc_annual_vol": {"default": 0.55, "env": "SIMMER_FASTLOOP_ANNUAL_VOL", "type": float,
                       "help": "Annualised volatility for N(d) fair-value model (default: 0.55 = 55%)"},
}

TRADE_SOURCE = "sdk:fastloop"
SKILL_SLUG = "polymarket-fast-loop"
_automaton_reported = False
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum
MAX_SPREAD_PCT = 0.10     # Skip if CLOB bid-ask spread exceeds this

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Asset → Gamma API search patterns
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}


from simmer_sdk.skill import load_config, update_config, get_config_path

# Load config
cfg = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-fast-loop")
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
_automaton_max = os.environ.get("AUTOMATON_MAX_BET")
if _automaton_max:
    MAX_POSITION_USD = min(MAX_POSITION_USD, float(_automaton_max))
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]  # "5m" or "15m"

# Dynamic min_time_remaining: 0 = auto (10% of window duration)
_window_seconds = {"5m": 300, "15m": 900, "1h": 3600}
_configured_min_time = cfg["min_time_remaining"]
if _configured_min_time > 0:
    MIN_TIME_REMAINING = _configured_min_time
else:
    MIN_TIME_REMAINING = max(30, _window_seconds.get(WINDOW, 300) // 10)
VOLUME_CONFIDENCE = cfg["volume_confidence"]
DAILY_BUDGET = cfg["daily_budget"]

# Fair-value mode: compare market price to Black-Scholes binary option fair value.
# fair_YES = N(d) where d = log(S/S0) / (σ_annual × √τ)
# S0 = BTC price at market open, S = current BTC price, τ = time remaining (years)
# Trades whichever side has the larger mispricing vs fair value.
USE_FAIR_VALUE = cfg["use_fair_value"]
FAIR_VALUE_MIN_EDGE = cfg["fair_value_min_edge"]
BTC_ANNUAL_VOL = cfg["btc_annual_vol"]
SECONDS_PER_YEAR = 31_536_000

# Polymarket crypto fee formula constants (from docs.polymarket.com/trading/fees)
# fee = C × p × POLY_FEE_RATE × (p × (1-p))^POLY_FEE_EXPONENT
POLY_FEE_RATE = 0.25       # Crypto markets
POLY_FEE_EXPONENT = 2      # Crypto markets


# =============================================================================
# Daily Budget Tracking
# =============================================================================

def _get_spend_path(skill_file):
    from pathlib import Path
    return Path(skill_file).parent / "daily_spend.json"


def _load_daily_spend(skill_file):
    """Load today's spend. Resets if date != today (UTC)."""
    spend_path = _get_spend_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if spend_path.exists():
        try:
            with open(spend_path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "spent": 0.0, "trades": 0}


def _save_daily_spend(skill_file, spend_data):
    """Save daily spend to file."""
    spend_path = _get_spend_path(skill_file)
    with open(spend_path, "w") as f:
        json.dump(spend_data, f, indent=2)


# =============================================================================
# API Helpers
# =============================================================================

_client = None

def get_client(live=True):
    """Lazy-init SimmerClient singleton."""
    global _client
    if _client is None:
        try:
            from simmer_sdk import SimmerClient
        except ImportError:
            print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
            sys.exit(1)
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            print("Get your API key from: simmer.markets/dashboard → SDK tab")
            sys.exit(1)
        venue = os.environ.get("TRADING_VENUE", "polymarket")
        _client = SimmerClient(api_key=api_key, venue=venue, live=live)
    return _client


def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    """Make an HTTP request to external APIs (Binance, CoinGecko, Gamma). Returns parsed JSON or None on error."""
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_market/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


CLOB_API = "https://clob.polymarket.com"


def _lookup_fee_rate(token_id):
    """Fetch taker fee rate (bps) from Polymarket CLOB for a token. Returns 0 on failure."""
    result = _api_request(f"{CLOB_API}/fee-rate?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return 0
    try:
        return int(float(result.get("base_fee") or 0))
    except (ValueError, TypeError):
        return 0


def fetch_live_midpoint(token_id):
    """Fetch live midpoint price from Polymarket CLOB for a single token."""
    result = _api_request(f"{CLOB_API}/midpoint?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return None
    try:
        return float(result["mid"])
    except (KeyError, ValueError, TypeError):
        return None


def fetch_live_prices(clob_token_ids):
    """Fetch live YES midpoint from Polymarket CLOB.

    Args:
        clob_token_ids: List of [yes_token_id, no_token_id] from Gamma.

    Returns:
        float or None: Live YES price (0-1).
    """
    if not clob_token_ids or len(clob_token_ids) < 1:
        return None
    yes_token = clob_token_ids[0]
    return fetch_live_midpoint(yes_token)


def fetch_orderbook_summary(clob_token_ids):
    """Fetch order book for YES token and return spread + depth summary.

    Args:
        clob_token_ids: List of [yes_token_id, no_token_id] from Gamma.

    Returns:
        dict with spread_pct, best_bid, best_ask, bid_depth_usd, ask_depth_usd
        or None on failure.
    """
    if not clob_token_ids or len(clob_token_ids) < 1:
        return None
    yes_token = clob_token_ids[0]
    result = _api_request(f"{CLOB_API}/book?token_id={quote(str(yes_token))}", timeout=5)
    if not result or not isinstance(result, dict):
        return None

    bids = result.get("bids", [])
    asks = result.get("asks", [])
    if not bids or not asks:
        return None

    try:
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2
        spread_pct = spread / mid if mid > 0 else 0

        # Sum depth (top 5 levels)
        bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
        }
    except (KeyError, ValueError, IndexError, TypeError):
        return None


# =============================================================================
# Sprint Market Discovery
# =============================================================================

# Asset prefix mapping for Unix-timestamp slug format
# e.g. btc-updown-5m-1773592800, eth-updown-5m-..., sol-updown-5m-...
_ASSET_SLUG_PREFIX = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}


def _lookup_by_unix_slug(asset="BTC", window="5m"):
    """
    MOST RELIABLE method: fetch the live window by computing its Unix-timestamp slug.

    Polymarket generates live market URLs as: {asset}-updown-{window}-{unix_ts}
    where unix_ts = (int(time.time()) // window_secs) * window_secs

    This never fails due to Simmer import lag or Gamma API filtering quirks
    because we construct the exact URL of the currently-open market.
    """
    import time as _time
    prefix      = _ASSET_SLUG_PREFIX.get(asset.upper(), "btc")
    window_secs = _window_seconds.get(window, 300)
    now_ts      = int(_time.time())
    win_ts      = (now_ts // window_secs) * window_secs

    markets = []
    # Check current and previous window (catches the first seconds of a new window)
    for ts in [win_ts, win_ts - window_secs]:
        slug = f"{prefix}-updown-{window}-{ts}"
        url  = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        data = _api_request(url, timeout=8)
        if not data or isinstance(data, dict):
            continue
        for m in data:
            if m.get("closed", False):
                continue
            clob_tokens_raw = m.get("clobTokenIds", "[]")
            if isinstance(clob_tokens_raw, str):
                try:
                    clob_tokens = json.loads(clob_tokens_raw)
                except Exception:
                    clob_tokens = []
            else:
                clob_tokens = clob_tokens_raw or []
            # Use Unix timestamp for exact open/close times — no title parsing needed
            opens_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            end_time = datetime.fromtimestamp(ts + window_secs, tz=timezone.utc)
            markets.append({
                "question":     m.get("question", slug),
                "slug":         slug,
                "condition_id": m.get("conditionId", ""),
                "end_time":     end_time,
                "opens_at":     opens_at,
                "clob_token_ids": clob_tokens,
                "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 1000),
                "source":       "clob_direct",
            })
            print(f"  ✅ Live window via slug: {m.get('question', slug)}")
            return markets  # Return on first hit
    return markets


def _gamma_current_window(asset="BTC", window="5m"):
    """
    Two-path live window discovery:
    1. Unix slug lookup (primary — always works, guaranteed correct window)
    2. endDateMin/endDateMax time query (fallback)
    """
    # Path 1: direct Unix timestamp slug — most reliable
    direct = _lookup_by_unix_slug(asset, window)
    if direct:
        return direct

    # Path 2: time-bounded Gamma query (fallback if slug lookup fails)
    from urllib.parse import urlencode
    now = datetime.now(timezone.utc)
    window_secs = _window_seconds.get(window, 300)
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(seconds=window_secs + 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])

    params = urlencode({
        "limit": 10,
        "closed": "false",
        "tag": "crypto",
        "endDateMin": end_min,
        "endDateMax": end_max,
    })
    url = f"https://gamma-api.polymarket.com/markets?{params}"
    result = _api_request(url, timeout=8)
    if not result or isinstance(result, dict):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        if not any(p in q for p in patterns):
            continue
        if m.get("closed", False):
            continue
        slug = m.get("slug", "")
        if not slug:
            continue
        end_time = _parse_fast_market_end_time(m.get("question", ""))
        if not end_time:
            continue
        clob_tokens_raw = m.get("clobTokenIds", "[]")
        if isinstance(clob_tokens_raw, str):
            try:
                clob_tokens = json.loads(clob_tokens_raw)
            except Exception:
                clob_tokens = []
        else:
            clob_tokens = clob_tokens_raw or []
        opens_at = end_time - timedelta(seconds=window_secs)
        markets.append({
            "question":     m.get("question", ""),
            "slug":         slug,
            "condition_id": m.get("conditionId", ""),
            "end_time":     end_time,
            "opens_at":     opens_at,
            "clob_token_ids": clob_tokens,
            "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
            "source":       "gamma_live",
        })
    return markets


def discover_fast_market_markets(asset="BTC", window="5m"):
    """Find active fast markets.
    Strategy:
      1. Query Gamma for markets expiring in the CURRENT window (live right now)
      2. Query Simmer for pre-imported upcoming markets (used for signal + import)
      3. Merge both — deduplicate by question title
    """
    now = datetime.now(timezone.utc)
    window_secs = _window_seconds.get(window, 300)
    all_markets = []
    seen_questions = set()

    # ── Step 1: Gamma current-window query (finds the live market NOW) ──────
    try:
        live_markets = _gamma_current_window(asset, window)
        if live_markets:
            print(f"  INFO: Gamma live query found {len(live_markets)} current-window market(s)")
        for m in live_markets:
            key = m["question"].lower()[:60]
            if key not in seen_questions:
                seen_questions.add(key)
                all_markets.append(m)
    except Exception as e:
        print(f"  WARNING: Gamma live query failed ({e})")

    # ── Step 2: Simmer pre-imported markets (upcoming windows) ───────────────
    try:
        client = get_client()
        sdk_markets = client.get_fast_markets(asset=asset, window=window, limit=100)
        if sdk_markets:
            simmer_count = 0
            for m in sdk_markets:
                end_time = _parse_resolves_at(m.resolves_at) if m.resolves_at else None
                opens_at = _parse_resolves_at(m.opens_at) if getattr(m, "opens_at", None) else None
                clob_tokens = [m.polymarket_token_id] if m.polymarket_token_id else []
                if m.polymarket_no_token_id:
                    clob_tokens.append(m.polymarket_no_token_id)
                key = (m.question or "").lower()[:60]
                if key not in seen_questions:
                    seen_questions.add(key)
                    all_markets.append({
                        "question":     m.question,
                        "market_id":    m.id,
                        "end_time":     end_time,
                        "opens_at":     opens_at,
                        "clob_token_ids": clob_tokens,
                        "is_live_now":  m.is_live_now,
                        "spread_cents": m.spread_cents,
                        "liquidity_tier": m.liquidity_tier,
                        "external_price_yes": m.external_price_yes,
                        "fee_rate_bps": getattr(m, "fee_rate_bps", 0),
                        "source":       "simmer",
                    })
                    simmer_count += 1
            print(f"  INFO: Simmer added {simmer_count} upcoming markets")
    except Exception as e:
        print(f"  WARNING: Simmer fast-markets API failed ({e}), using Gamma only")
        if not all_markets:
            # Full Gamma fallback
            return _discover_via_gamma(asset, window)

    return all_markets


def _discover_via_gamma(asset="BTC", window="5m"):
    """Fallback: Find active fast markets on Polymarket via Gamma API."""
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=100&closed=false&tag=crypto&order=endDate&ascending=true"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        # Do NOT filter by "-5m-" in slug — the currently-live window uses a
        # different slug format (e.g. "bitcoin-up-or-down-march-15-1125am-et")
        # that never contains "-5m-". Match by question title only.
        if any(p in q for p in patterns):
            condition_id = m.get("conditionId", "")
            closed = m.get("closed", False)
            if not closed and slug:
                end_time = _parse_fast_market_end_time(m.get("question", ""))
                clob_tokens_raw = m.get("clobTokenIds", "[]")
                if isinstance(clob_tokens_raw, str):
                    try:
                        clob_tokens = json.loads(clob_tokens_raw)
                    except (json.JSONDecodeError, ValueError):
                        clob_tokens = []
                else:
                    clob_tokens = clob_tokens_raw or []
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": condition_id,
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "clob_token_ids": clob_tokens,
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                    "source": "gamma",
                })
    return markets


def _parse_resolves_at(resolves_at_str):
    """Parse a resolves_at string (ISO format) into a timezone-aware UTC datetime."""
    try:
        # Handle both "2026-03-02 05:10:00Z" and "2026-03-02T05:10:00Z" formats
        s = resolves_at_str.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_fast_market_end_time(question):
    """Parse end time from fast market question (Gamma fallback path).
    e.g., 'Bitcoin Up or Down - February 15, 5:30AM-5:35AM ET' → datetime
    """
    import re
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        from zoneinfo import ZoneInfo
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        et = ZoneInfo("America/New_York")
        dt = dt.replace(tzinfo=et).astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def find_best_fast_market(markets):
    """Pick the best fast_market to trade: live now, soonest expiring, enough time remaining."""
    now = datetime.now(timezone.utc)
    max_remaining = _window_seconds.get(WINDOW, 300) * 2
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining <= MIN_TIME_REMAINING:
            continue  # Too close to expiry

        # Determine start_time — prefer opens_at from Simmer API (exact),
        # fall back to computing from window duration (Gamma path)
        opens_at = m.get("opens_at")
        if opens_at:
            start_time = opens_at
        else:
            window_secs = _window_seconds.get(WINDOW, 300)
            start_time = end_time - timedelta(seconds=window_secs)

        # Market is live if we are inside its trading window
        if start_time <= now:
            candidates.append((remaining, m))
        # else: market opens in the future — skip

    if not candidates:
        return None
    # Sort by soonest expiring
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# =============================================================================
# CEX Price Signal
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """Get price momentum from Binance public API.
    Returns: {momentum_pct, direction, price_now, price_then, avg_volume, candles}
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        # Kline format: [open_time, open, high, low, close, volume, ...]
        candles = result
        if len(candles) < 2:
            return None

        price_then = float(candles[0][1])   # open of oldest candle
        price_now = float(candles[-1][4])    # close of newest candle
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]

        # Volume ratio: latest vs average (>1 = above average activity)
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
        }
    except (IndexError, ValueError, KeyError):
        return None


COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


# Additional exchange symbols for fallback
_BINANCE_US_SYMBOLS = {"BTC": "BTCUSD",  "ETH": "ETHUSD",  "SOL": "SOLUSD"}
_OKX_SYMBOLS        = {"BTC": "BTC-USDT","ETH": "ETH-USDT","SOL": "SOL-USDT"}
_BYBIT_SYMBOLS      = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
_KRAKEN_SYMBOLS_MOM = {"BTC": "XBTUSD",  "ETH": "ETHUSD",  "SOL": "SOLUSD"}


def _build_momentum_result(price_then, price_now, volumes):
    avg_vol   = sum(volumes) / len(volumes) if volumes else 1
    last_vol  = volumes[-1] if volumes else 1
    pct       = ((price_now - price_then) / price_then) * 100
    return {"momentum_pct": pct, "direction": "up" if pct > 0 else "down",
            "price_now": price_now, "price_then": price_then,
            "avg_volume": avg_vol, "latest_volume": last_vol,
            "volume_ratio": last_vol / avg_vol if avg_vol > 0 else 1.0,
            "candles": len(volumes)}


def _get_binance_us_momentum(asset, lookback):
    symbol = _BINANCE_US_SYMBOLS.get(asset, "BTCUSD")
    url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback}"
    r = _api_request(url)
    if not r or isinstance(r, dict): return None
    try:
        return _build_momentum_result(float(r[0][1]), float(r[-1][4]), [float(c[5]) for c in r])
    except Exception: return None


def _get_okx_momentum(asset, lookback):
    symbol = _OKX_SYMBOLS.get(asset, "BTC-USDT")
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit={lookback}"
    r = _api_request(url)
    if not r or r.get("code") != "0": return None
    candles = list(reversed(r.get("data", [])))
    if len(candles) < 2: return None
    try:
        return _build_momentum_result(float(candles[0][1]), float(candles[-1][4]), [float(c[5]) for c in candles])
    except Exception: return None


def _get_bybit_momentum(asset, lookback):
    symbol = _BYBIT_SYMBOLS.get(asset, "BTCUSDT")
    url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval=1&limit={lookback}"
    r = _api_request(url)
    if not r or r.get("retCode") != 0: return None
    candles = list(reversed(r.get("result", {}).get("list", [])))
    if len(candles) < 2: return None
    try:
        return _build_momentum_result(float(candles[0][1]), float(candles[-1][4]), [float(c[5]) for c in candles])
    except Exception: return None


def _get_kraken_momentum(asset, lookback):
    import time as _time
    symbol = _KRAKEN_SYMBOLS_MOM.get(asset, "XBTUSD")
    since  = int(_time.time()) - lookback * 60 - 60
    url    = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=1&since={since}"
    r = _api_request(url)
    if not r or r.get("error"): return None
    keys = [k for k in r.get("result", {}) if k != "last"]
    if not keys: return None
    candles = r["result"][keys[0]]
    if len(candles) < 2: return None
    try:
        return _build_momentum_result(float(candles[0][1]), float(candles[-1][4]), [float(c[6]) for c in candles])
    except Exception: return None


def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum — tries Binance first, falls back through 4 other sources."""
    symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
    attempts = [
        ("binance",    lambda: get_binance_momentum(symbol, lookback)),
        ("binance.us", lambda: _get_binance_us_momentum(asset, lookback)),
        ("okx",        lambda: _get_okx_momentum(asset, lookback)),
        ("bybit",      lambda: _get_bybit_momentum(asset, lookback)),
        ("kraken",     lambda: _get_kraken_momentum(asset, lookback)),
    ]
    for name, fn in attempts:
        try:
            result = fn()
            if result:
                if name != "binance":
                    print(f"  ℹ️  Price source: {name} (binance unavailable)", flush=True)
                return result
        except Exception:
            pass
    return None


# =============================================================================
# Fair-Value Model (Black-Scholes binary option)
# =============================================================================

def _norm_cdf(x):
    """Standard normal CDF — Abramowitz & Stegun rational approximation.
    Max error < 7.5e-8. No external dependencies."""
    import math
    a1, a2, a3, a4, a5 = 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    k = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))))
    n = 1.0 - math.exp(-0.5 * x * x) * poly / math.sqrt(2 * math.pi)
    return n if x >= 0 else 1.0 - n


def get_binance_price_at(symbol, start_ms):
    """Get BTC close price of the 1-minute candle starting at start_ms (unix ms).
    Used to fetch the reference price at market open for the N(d) model."""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&startTime={start_ms}&limit=1"
    )
    result = _api_request(url)
    if isinstance(result, list) and len(result) > 0:
        return float(result[0][4])  # close price
    return None


# =============================================================================
# Import & Trade
# =============================================================================

def import_fast_market_market(slug):
    """Import a fast market to Simmer. Returns market_id or None."""
    url = f"https://polymarket.com/event/{slug}"
    try:
        result = get_client().import_market(url)
    except Exception as e:
        return None, str(e)

    if not result:
        return None, "No response from import endpoint"

    if result.get("error"):
        return None, result.get("error", "Unknown error")

    status = result.get("status")
    market_id = result.get("market_id")

    if status == "resolved":
        alternatives = result.get("active_alternatives", [])
        if alternatives:
            return None, f"Market resolved. Try alternative: {alternatives[0].get('id')}"
        return None, "Market resolved, no alternatives found"

    if status in ("imported", "already_exists"):
        return market_id, None

    return None, f"Unexpected status: {status}"


def get_market_details(market_id):
    """Fetch market details by ID."""
    try:
        market = get_client().get_market_by_id(market_id)
        if not market:
            return None
        from dataclasses import asdict
        return asdict(market)
    except Exception:
        return None


def get_portfolio():
    """Get portfolio summary."""
    try:
        return get_client().get_portfolio()
    except Exception as e:
        return {"error": str(e)}


def get_positions():
    """Get current positions as list of dicts."""
    try:
        positions = get_client().get_positions()
        from dataclasses import asdict
        return [asdict(p) for p in positions]
    except Exception:
        return []


def execute_trade(market_id, side, amount):
    """Execute a trade on Simmer."""
    try:
        result = get_client().trade(
            market_id=market_id,
            side=side,
            amount=amount,
            source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
        )
        return {
            "success": result.success,
            "trade_id": result.trade_id,
            "shares_bought": result.shares_bought,
            "shares": result.shares_bought,
            "error": result.error,
            "simulated": result.simulated,
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_position_size(max_size, smart_sizing=False):
    """Calculate position size, optionally based on portfolio."""
    if not smart_sizing:
        return max_size
    portfolio = get_portfolio()
    if not portfolio or portfolio.get("error"):
        return max_size
    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        return max_size
    smart_size = balance * SMART_SIZING_PCT
    return min(smart_size, max_size)


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_fast_market_strategy(dry_run=True, positions_only=False, show_config=False,
                        smart_sizing=False, quiet=False):
    """Run one cycle of the fast_market trading strategy."""

    def log(msg, force=False):
        """Print unless quiet mode is on. force=True always prints."""
        if not quiet or force:
            print(msg)

    log("⚡ Simmer FastLoop Trading Skill")
    log("=" * 50)

    if dry_run:
        log("\n  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.")

    log(f"\n⚙️  Configuration:")
    log(f"  Asset:            {ASSET}")
    log(f"  Window:           {WINDOW}")
    log(f"  Entry threshold:  {ENTRY_THRESHOLD} (min divergence from 50¢)")
    log(f"  Min momentum:     {MIN_MOMENTUM_PCT}% (min price move)")
    log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    log(f"  Signal source:    {SIGNAL_SOURCE}")
    log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    log(f"  Min time left:    {MIN_TIME_REMAINING}s")
    log(f"  Volume weighting: {'✓' if VOLUME_CONFIDENCE else '✗'}")
    daily_spend = _load_daily_spend(__file__)
    log(f"  Daily budget:     ${DAILY_BUDGET:.2f} (${daily_spend['spent']:.2f} spent today, {daily_spend['trades']} trades)")

    if show_config:
        config_path = get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"\n  To change settings:")
        log(f'    python fast_trader.py --set entry_threshold=0.08')
        log(f'    python fast_trader.py --set asset=ETH')
        log(f'    Or edit config.json directly')
        return

    # Initialize client early to validate API key (paper mode when not live)
    get_client(live=not dry_run)

    # Show positions if requested
    if positions_only:
        log("\n📊 Sprint Positions:")
        positions = get_positions()
        fast_market_positions = [p for p in positions if "up or down" in (p.get("question", "") or "").lower()]
        if not fast_market_positions:
            log("  No open fast market positions")
        else:
            for pos in fast_market_positions:
                log(f"  • {pos.get('question', 'Unknown')[:60]}")
                log(f"    YES: {pos.get('shares_yes', 0):.1f} | NO: {pos.get('shares_no', 0):.1f} | P&L: ${pos.get('pnl', 0):.2f}")
        return

    # Show portfolio if smart sizing
    if smart_sizing:
        log("\n💰 Portfolio:")
        portfolio = get_portfolio()
        if portfolio and not portfolio.get("error"):
            log(f"  Balance: ${portfolio.get('balance_usdc', 0):.2f}")

    # Step 1: Discover fast markets
    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")
    # Debug: show how many have opens_at and how many are currently live
    now_utc = datetime.now(timezone.utc)
    live_count = sum(1 for m in markets if m.get("opens_at") and m["opens_at"] <= now_utc)
    with_opens = sum(1 for m in markets if m.get("opens_at"))
    log(f"  opens_at available: {with_opens}/{len(markets)}  |  currently live: {live_count}")

    # Look up fee rate once per run from a sample token (same window = same fee tier)
    if markets:
        sample = next((m for m in markets if m.get("clob_token_ids")), None)
        if sample and sample.get("fee_rate_bps", 0) == 0:
            fee = _lookup_fee_rate(sample["clob_token_ids"][0])
            if fee > 0:
                log(f"  Fee rate for {WINDOW} markets: {fee} bps ({fee/100:.0f}%)")
                for m in markets:
                    m["fee_rate_bps"] = fee

    if not markets:
        log("  No active fast markets found — may be outside market hours or wrong asset/window")
        log(f"  Check: asset={ASSET}, window={WINDOW}")
        if not quiet:
            print("📊 Summary: No markets available")
        return

    # Step 2: Find best fast_market to trade
    best = find_best_fast_market(markets)
    if not best:
        now = datetime.now(timezone.utc)
        window_secs = _window_seconds.get(WINDOW, 300)
        live_count = 0
        for m in markets:
            end_time = m.get("end_time")
            if not end_time:
                continue
            remaining = (end_time - now).total_seconds()
            opens_at  = m.get("opens_at")
            start_time = opens_at if opens_at else end_time - timedelta(seconds=window_secs)
            is_live   = start_time <= now
            if is_live and remaining <= MIN_TIME_REMAINING:
                log(f"  Skipped: {m['question'][:50]}... ({remaining:.0f}s left, too close to expiry)")
            elif not is_live:
                opens_in = (start_time - now).total_seconds()
                log(f"  Skipped: {m['question'][:50]}... (opens in {opens_in:.0f}s)")
            else:
                live_count += 1
        log(f"  No live tradeable markets among {len(markets)} found (live={live_count}, all future or expired)")
        if not quiet:
            print(f"📊 Summary: No tradeable markets (0/{len(markets)} live with enough time)")
        return

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    # Dedup: skip if we already hold a position on this market
    _mid = best.get("market_id") or ""
    _q = best.get("question", "").lower()
    existing = get_positions()
    for pos in existing:
        held = (pos.get("shares_yes") or 0) + (pos.get("shares_no") or 0)
        if held <= 0:
            continue
        if (_mid and pos.get("market_id") == _mid) or (_q and pos.get("question", "").lower() == _q):
            log(f"  ⏸️  Already holding position on this market — skip (dedup)")
            if not quiet:
                print(f"📊 Summary: No trade (already holding this market)")
            skip_reasons.append("already holding")
            _emit_skip_report()
            return

    # Fetch live CLOB price — required for fast markets (stale prices cause bad trades)
    clob_tokens = best.get("clob_token_ids", [])
    live_price = fetch_live_prices(clob_tokens) if clob_tokens else None
    if live_price is not None:
        market_yes_price = live_price
        log(f"  Current YES price: ${market_yes_price:.3f} (live CLOB)")
    else:
        log(f"  ⏸️  Could not fetch live CLOB price — skipping (stale prices are unsafe on fast markets)")
        if not quiet:
            print(f"📊 Summary: No trade (CLOB price unavailable)")
        return

    # Fee info: Polymarket crypto fee formula (docs.polymarket.com/trading/fees):
    # fee = C × p × POLY_FEE_RATE × (p × (1-p))^POLY_FEE_EXPONENT
    # Max effective rate: 1.56% at 50¢. fee_rate_bps from API is a contract param,
    # not a direct percentage — we use the documented formula constants instead.
    fee_rate_bps = best.get("fee_rate_bps", 0)
    if fee_rate_bps > 0:
        # Effective fee at current market price using Polymarket crypto formula
        _p = market_yes_price if market_yes_price <= 0.5 else (1 - market_yes_price)
        _eff = POLY_FEE_RATE * (_p * (1 - _p)) ** POLY_FEE_EXPONENT
        log(f"  Fee rate:         {_eff:.2%} effective at current price (feeRateBps={fee_rate_bps})")

    # Step 3: Get CEX price momentum
    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)

    if not momentum:
        log("  ❌ Failed to fetch price data", force=True)
        return

    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    # Step 4: Decision logic
    log(f"\n🧠 Analyzing...")

    momentum_pct = abs(momentum["momentum_pct"])
    direction = momentum["direction"]
    skip_reasons = []

    def _emit_skip_report(signals=1, attempted=0):
        """Emit automaton JSON with skip_reason before early return."""
        global _automaton_reported
        if os.environ.get("AUTOMATON_MANAGED") and skip_reasons:
            report = {"signals": signals, "trades_attempted": attempted, "trades_executed": 0,
                      "skip_reason": ", ".join(dict.fromkeys(skip_reasons))}
            print(json.dumps({"automaton": report}))
            _automaton_reported = True

    # Check order book spread and depth
    # Use pre-fetched spread from Simmer API if available, otherwise fetch from CLOB
    pre_spread = best.get("spread_cents")
    if pre_spread is not None:
        # spread_cents is raw cents (e.g. 2.5 = 2.5¢). Convert to fraction of midpoint
        # for comparison with MAX_SPREAD_PCT. Fast markets trade near 50¢ midpoint.
        mid_estimate = market_yes_price if market_yes_price > 0 else 0.5
        spread_pct = (pre_spread / 100.0) / mid_estimate
        log(f"  Spread: {pre_spread:.1f}¢ ({best.get('liquidity_tier', 'unknown')})")
        if spread_pct > MAX_SPREAD_PCT:
            log(f"  ⏸️  Spread {spread_pct:.1%} > 10% — illiquid, skip")
            if not quiet:
                print(f"📊 Summary: No trade (wide spread: {spread_pct:.1%})")
            skip_reasons.append("wide spread")
            _emit_skip_report()
            return
    else:
        book = fetch_orderbook_summary(clob_tokens) if clob_tokens else None
        if book:
            log(f"  Spread: {book['spread_pct']:.1%} (bid ${book['best_bid']:.3f} / ask ${book['best_ask']:.3f})")
            log(f"  Depth: ${book['bid_depth_usd']:.0f} bid / ${book['ask_depth_usd']:.0f} ask (top 5)")
            if book["spread_pct"] > MAX_SPREAD_PCT:
                log(f"  ⏸️  Spread {book['spread_pct']:.1%} > 10% — illiquid, skip")
                if not quiet:
                    print(f"📊 Summary: No trade (wide spread: {book['spread_pct']:.1%})")
                skip_reasons.append("wide spread")
                _emit_skip_report()
                return

    # Check minimum momentum (loose gate when fair-value mode is on — edge check filters there)
    _momentum_floor = 0.01 if USE_FAIR_VALUE else MIN_MOMENTUM_PCT
    if momentum_pct < _momentum_floor:
        log(f"  ⏸️  Momentum {momentum_pct:.3f}% < minimum {_momentum_floor}% — skip")
        if not quiet:
            print(f"📊 Summary: No trade (momentum too weak: {momentum_pct:.3f}%)")
        return

    # Decision logic: fair-value model or raw momentum
    if USE_FAIR_VALUE:
        # ── N(d) fair-value model ─────────────────────────────────────────────
        # Treats the fast market as a binary option expiring at market close.
        # fair_YES = N(d) where d = log(S/S0) / (σ_annual × √τ)
        #   S0 = BTC price at market open  (fetched from Binance klines)
        #   S  = BTC price now             (from momentum signal)
        #   σ  = btc_annual_vol config param
        #   τ  = seconds remaining / SECONDS_PER_YEAR
        # Trade whichever direction (YES/NO) has the larger mispricing vs fair value.
        import math
        _wdur = _window_seconds.get(WINDOW, 300)
        _fv_symbol = ASSET_SYMBOLS.get(ASSET, "BTCUSDT")
        _btc_start_price = None
        if end_time:
            _market_open_ms = int((end_time.timestamp() - _wdur) * 1000)
            _btc_start_price = get_binance_price_at(_fv_symbol, _market_open_ms)

        if _btc_start_price and _btc_start_price > 0 and remaining > 30:
            _log_ret = math.log(momentum["price_now"] / _btc_start_price)
            _sigma_tau = BTC_ANNUAL_VOL * math.sqrt(remaining / SECONDS_PER_YEAR)
            _d = _log_ret / _sigma_tau if _sigma_tau > 0 else 0
            fair_yes = _norm_cdf(_d)
            edge = fair_yes - market_yes_price

            log(f"  BTC at open:  ${_btc_start_price:,.2f}  →  now ${momentum['price_now']:,.2f}")
            log(f"  Fair YES: {fair_yes:.3f}  |  Market: {market_yes_price:.3f}  |  Edge: {edge:+.3f}  (d={_d:.2f})")

            if abs(edge) < FAIR_VALUE_MIN_EDGE:
                log(f"  ⏸️  Edge {abs(edge):.3f} < min {FAIR_VALUE_MIN_EDGE} — skip")
                if not quiet:
                    print(f"📊 Summary: No trade (edge {edge:+.3f} below threshold {FAIR_VALUE_MIN_EDGE})")
                skip_reasons.append("insufficient edge")
                _emit_skip_report()
                return

            side = "yes" if edge > 0 else "no"
            divergence = abs(edge)
            trade_rationale = (
                f"fair YES={fair_yes:.3f} vs market={market_yes_price:.3f}"
                f" ({edge:+.3f} edge, d={_d:.2f})"
            )
        else:
            # Fallback to momentum when BTC start price is unavailable or <30s left
            log("  ⚠️  Fair-value unavailable (no start price or <30s left) — momentum fallback")
            if direction == "up":
                side = "yes"
                divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
                trade_rationale = f"momentum fallback: {ASSET} {momentum['momentum_pct']:+.3f}%"
            else:
                side = "no"
                divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
                trade_rationale = f"momentum fallback: {ASSET} {momentum['momentum_pct']:+.3f}%"
            if divergence <= 0:
                log(f"  ⏸️  Fallback divergence {divergence:.3f} ≤ 0 — skip")
                if not quiet:
                    print(f"📊 Summary: No trade (fallback: market priced in)")
                skip_reasons.append("market already priced in")
                _emit_skip_report()
                return
    else:
        # Default: follow the momentum direction.
        # Simple model: strong momentum → higher probability of continuation
        if direction == "up":
            side = "yes"
            divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
            trade_rationale = f"{ASSET} up {momentum['momentum_pct']:+.3f}% but YES only ${market_yes_price:.3f}"
        else:
            side = "no"
            divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
            trade_rationale = f"{ASSET} down {momentum['momentum_pct']:+.3f}% but YES still ${market_yes_price:.3f}"

    # Volume confidence adjustment
    vol_note = ""
    if VOLUME_CONFIDENCE and momentum["volume_ratio"] < 0.5:
        log(f"  ⏸️  Low volume ({momentum['volume_ratio']:.2f}x avg) — weak signal, skip")
        if not quiet:
            print(f"📊 Summary: No trade (low volume)")
        skip_reasons.append("low volume")
        _emit_skip_report()
        return
    elif VOLUME_CONFIDENCE and momentum["volume_ratio"] > 2.0:
        vol_note = f" 📊 (high volume: {momentum['volume_ratio']:.1f}x avg)"

    # Check divergence threshold
    if divergence <= 0:
        log(f"  ⏸️  Market already priced in: divergence {divergence:.3f} ≤ 0 — skip")
        if not quiet:
            print(f"📊 Summary: No trade (market already priced in)")
        skip_reasons.append("market already priced in")
        _emit_skip_report()
        return

    # Fee-aware EV check: require enough divergence to cover fees
    # EV = win_prob * payout_after_fees - (1 - win_prob) * cost
    # At the buy price, win_prob ≈ buy_price (market-implied).
    # We need our edge (divergence) to overcome the fee drag.
    if fee_rate_bps > 0:
        buy_price = market_yes_price if side == "yes" else (1 - market_yes_price)
        # Polymarket crypto fee: fee = C × p × 0.25 × (p × (1-p))^2
        # Effective rate = 0.25 × (p × (1-p))^2. Fee per share = buy_price × eff_rate.
        effective_fee_rate = POLY_FEE_RATE * (buy_price * (1 - buy_price)) ** POLY_FEE_EXPONENT
        fee_per_share = buy_price * effective_fee_rate  # absolute fee in price terms
        # Divergence is in absolute price — compare to fee drag + buffer
        min_divergence = fee_per_share * 2 + 0.02  # round-trip fee + buffer
        log(f"  Fee:              ${fee_per_share:.4f}/share ({effective_fee_rate:.2%} effective, min divergence {min_divergence:.3f})")
        if divergence < min_divergence:
            log(f"  ⏸️  Divergence {divergence:.3f} < fee-adjusted minimum {min_divergence:.3f} — skip")
            if not quiet:
                print(f"📊 Summary: No trade (fees eat the edge)")
            skip_reasons.append("fees eat the edge")
            _emit_skip_report()
            return

    # We have a signal!
    position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing)
    price = market_yes_price if side == "yes" else (1 - market_yes_price)

    # Daily budget check
    remaining_budget = DAILY_BUDGET - daily_spend["spent"]
    if remaining_budget <= 0:
        log(f"  ⏸️  Daily budget exhausted (${daily_spend['spent']:.2f}/${DAILY_BUDGET:.2f} spent) — skip")
        if not quiet:
            print(f"📊 Summary: No trade (daily budget exhausted)")
        skip_reasons.append("daily budget exhausted")
        _emit_skip_report()
        return
    if position_size > remaining_budget:
        position_size = remaining_budget
        log(f"  Budget cap: trade capped at ${position_size:.2f} (${daily_spend['spent']:.2f}/${DAILY_BUDGET:.2f} spent)")
    if position_size < 0.50:
        log(f"  ⏸️  Remaining budget ${position_size:.2f} < $0.50 — skip")
        if not quiet:
            print(f"📊 Summary: No trade (remaining budget too small)")
        skip_reasons.append("budget too small")
        _emit_skip_report()
        return

    # Check minimum order size
    if price > 0:
        min_cost = MIN_SHARES_PER_ORDER * price
        if min_cost > position_size:
            log(f"  ⚠️  Position ${position_size:.2f} too small for {MIN_SHARES_PER_ORDER} shares at ${price:.2f}")
            skip_reasons.append("position too small")
            _emit_skip_report(attempted=1)
            return

    log(f"  ✅ Signal: {side.upper()} — {trade_rationale}{vol_note}", force=True)
    log(f"  Divergence: {divergence:.3f}", force=True)

    # Step 5: Get market ID (already have it from Simmer API, or import from Gamma)
    if best.get("market_id"):
        market_id = best["market_id"]
        log(f"\n🔗 Market ready: {market_id[:16]}...", force=True)
    else:
        log(f"\n🔗 Importing to Simmer...", force=True)
        market_id, import_error = import_fast_market_market(best["slug"])
        if not market_id:
            log(f"  ❌ Import failed: {import_error}", force=True)
            return
        log(f"  ✅ Market ID: {market_id[:16]}...", force=True)

    execution_error = None
    tag = "SIMULATED" if dry_run else "LIVE"
    log(f"  Executing {side.upper()} trade for ${position_size:.2f} ({tag})...", force=True)
    result = execute_trade(market_id, side, position_size)

    if result and result.get("success"):
        shares = result.get("shares_bought") or result.get("shares") or 0
        trade_id = result.get("trade_id")
        log(f"  ✅ {'[PAPER] ' if result.get('simulated') else ''}Bought {shares:.1f} {side.upper()} shares @ ${price:.3f}", force=True)

        # Update daily spend (skip for paper trades)
        if not result.get("simulated"):
            daily_spend["spent"] += position_size
            daily_spend["trades"] += 1
            _save_daily_spend(__file__, daily_spend)

        # Log to trade journal (skip for paper trades)
        if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
            confidence = min(0.9, 0.5 + divergence + (momentum_pct / 100))
            log_trade(
                trade_id=trade_id,
                source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                thesis=trade_rationale,
                confidence=round(confidence, 2),
                asset=ASSET,
                momentum_pct=round(momentum["momentum_pct"], 3),
                volume_ratio=round(momentum["volume_ratio"], 2),
                signal_source=SIGNAL_SOURCE,
            )
    else:
        error = result.get("error", "Unknown error") if result else "No response"
        log(f"  ❌ Trade failed: {error}", force=True)
        execution_error = error[:120]

    # Summary
    total_trades = 1 if result and result.get("success") else 0
    show_summary = not quiet or total_trades > 0
    if show_summary:
        print(f"\n📊 Summary:")
        print(f"  Sprint: {best['question'][:50]}")
        print(f"  Signal: {direction} {momentum_pct:.3f}% | YES ${market_yes_price:.3f}")
        print(f"  Action: {'PAPER' if dry_run else ('TRADED' if total_trades else 'FAILED')}")

    # Structured report for automaton (takes priority over fallback in __main__)
    if os.environ.get("AUTOMATON_MANAGED"):
        global _automaton_reported
        amount = round(position_size, 2) if total_trades > 0 else 0
        report = {"signals": 1, "trades_attempted": 1, "trades_executed": total_trades, "amount_usd": amount}
        if execution_error:
            report["execution_errors"] = [execution_error]
        print(json.dumps({"automaton": report}))
        _automaton_reported = True


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true", help="Show current fast market positions")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only output on trades/errors (ideal for high-frequency runs)")
    args = parser.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key in CONFIG_SCHEMA:
                type_fn = CONFIG_SCHEMA[key].get("type", str)
                try:
                    if type_fn == bool:
                        updates[key] = val.lower() in ("true", "1", "yes")
                    else:
                        updates[key] = type_fn(val)
                except ValueError:
                    print(f"Invalid value for {key}: {val}")
                    sys.exit(1)
            else:
                print(f"Unknown config key: {key}")
                print(f"Valid keys: {', '.join(CONFIG_SCHEMA.keys())}")
                sys.exit(1)
        result = update_config(updates, __file__)
        print(f"✅ Config updated: {json.dumps(updates)}")
        sys.exit(0)

    dry_run = not args.live

    run_fast_market_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        quiet=args.quiet,
    )

    # Fallback report for automaton if the strategy returned early (no signal)
    # The function emits its own report when it reaches a trade; this covers early exits.
    if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
        print(json.dumps({"automaton": {"signals": 0, "trades_attempted": 0, "trades_executed": 0, "skip_reason": "no_signal"}}))
