# ⚡ FastLoop Trader — Railway Deployment

Polymarket BTC 5-minute fast market trader, running as a Railway cron job every 5 minutes.

---

## 🚀 Deploy to Railway (Step-by-Step)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "FastLoop Trader initial deploy"
git remote add origin https://github.com/YOUR_USERNAME/fastloop-trader.git
git push -u origin main
```

### 2. Create Railway Project

1. Go to [railway.app](https://railway.app) → **New Project**
2. Select **Deploy from GitHub repo**
3. Choose your `fastloop-trader` repository
4. Railway will auto-detect the `Dockerfile` and `railway.toml`

### 3. Set Environment Variables

In Railway → your service → **Variables** tab, add all variables from `.env.example`:

| Variable | Value |
|---|---|
| `SIMMER_API_KEY` | From simmer.markets/dashboard → SDK tab |
| `WALLET_PRIVATE_KEY` | Your Polymarket wallet private key |
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `@Jlichbot` or your numeric chat ID |
| `LIVE_TRADING` | `0` (dry-run) or `1` (live) |
| `SIMMER_SPRINT_ASSET` | `BTC` |
| `DAILY_BUDGET_USD` | `20` |

> ⚠️ **Never push your `.env` file to Git.** Use Railway's Variables UI only.

### 4. Verify Cron Schedule

In Railway → your service → **Settings**, confirm:
- **Start Command**: `python run.py`
- **Cron Schedule**: `*/5 * * * *` (from `railway.toml`)

### 5. Watch Logs

Railway → your service → **Logs** tab. You'll see output like:

```
[2026-03-14 10:00 UTC] ⚡ FastLoop cycle starting (DRY RUN)
⚡ Simmer FastLoop Trading Skill
🔍 Discovering BTC fast markets...
  Found 2 active fast markets
📊 Summary: No trade (momentum too weak: 0.312%)
[2026-03-14 10:00 UTC] ✅ FastLoop cycle complete
```

---

## 🔴 Going Live

When you're ready to trade real USDC:

1. In Railway → Variables → change `LIVE_TRADING` from `0` to `1`
2. Redeploy (Railway auto-redeploys on variable changes)
3. You'll get a Telegram alert on the next trade

---

## 📱 Telegram Alerts

You'll receive alerts for:
- ✅ Trade executed (live or dry-run)
- ⚠️ Execution errors
- 💰 Budget >80% consumed for the day

To also get skipped-cycle pings (noisy), set `NOTIFY_SKIPS=1`.

---

## ⚙️ Tuning Parameters

Change these in Railway Variables without redeploying:

| Variable | Default | Description |
|---|---|---|
| `SIMMER_FASTLOOP_ENTRY_THRESHOLD` | `0.05` | Min divergence from 50¢ to trade |
| `SIMMER_FASTLOOP_MOMENTUM_THRESHOLD` | `0.5` | Min BTC % move to trigger |
| `SIMMER_FASTLOOP_MAX_POSITION_USD` | `5` | Max $ per individual trade |
| `SIMMER_FASTLOOP_LOOKBACK_MINUTES` | `5` | Minutes of price history |
| `DAILY_BUDGET_USD` | `20` | Max spend per UTC day |

---

## ⚠️ Risk Reminders

- Polymarket fast markets carry a **10% fee** (`is_paid: true`) — factor into edge
- Stop-loss monitors **do not apply** to sub-15-minute markets
- Start with dry-run and monitor logs for at least one day before going live
- Daily budget resets at **UTC midnight**

---

## 📁 File Structure

```
fastloop-trader/
├── fastloop_trader.py   # Core trading logic (Simmer skill — do not modify)
├── run.py               # Railway entrypoint + Telegram notification wrapper
├── telegram_notify.py   # Telegram alert functions
├── config.json          # Strategy config (can also use env vars)
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container build
├── railway.toml         # Railway cron schedule config
├── .env.example         # Template for Railway Variables
└── .gitignore
```
