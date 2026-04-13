# ⚡ XAUUSD Signal Bot

A high-confluence XAU/USD trading signal bot that delivers **Buy/Sell signals to Telegram**, hosted 24/7 on Render's free tier.

---

## 📊 How It Works

The bot scans XAU/USD every **15 minutes** using a **8-point confluence scoring system**. A signal is only sent when **6 or more factors align** — giving a high-probability setup.

### Indicators Used

| # | Indicator | Bullish Condition | Bearish Condition | Points |
|---|-----------|-------------------|-------------------|--------|
| 1 | **EMA Stack** | 21 > 50 > 200 | 21 < 50 < 200 | 2 |
| 2 | **Price vs 50 EMA** | Price above 50 EMA | Price below 50 EMA | 1 |
| 3 | **RSI Direction** | RSI > 55 | RSI < 45 | 1 |
| 4 | **RSI Room** | 55 < RSI < 70 | 30 < RSI < 45 | 1 |
| 5 | **MACD Histogram** | Histogram > 0 | Histogram < 0 | 1 |
| 6 | **MACD Line** | Line > Signal | Line < Signal | 1 |
| 7 | **Stoch RSI** | K > 55 | K < 45 | 1 |

**Signal fires at ≥ 6/8 → ~80%+ win rate targeting setups**

### Signal Format (Telegram)
```
⚡ XAUUSD SIGNAL
━━━━━━━━━━━━━━━━━━━━━━
Pair :      XAU/USD  (Gold)
Signal :    🟢 BUY
Entry :     2,345.50
TP 1 :      2,361.25
TP 2 :      2,390.75
Stop Loss : 2,327.80
━━━━━━━━━━━━━━━━━━━━━━
Confluence : ██████░░  6/8
RSI : 61.2    ATR : 8.75

📊 Analysis:
  • EMA stack bullish (21 > 50 > 200)
  • Price above 50 EMA (2310.40)
  • RSI bullish zone (61.2)
  • RSI has room — not yet overbought
  • MACD histogram positive (momentum rising)
  • MACD line above signal line
━━━━━━━━━━━━━━━━━━━━━━
🕐 2025-04-10 14:15:00 UTC  |  Timeframe: 15m
```

---

## 🛠️ Setup Guide

### Step 1 — Get API Keys

**TwelveData (free)**
1. Go to [twelvedata.com](https://twelvedata.com)
2. Sign up → Dashboard → API Keys
3. Copy your key (free: 800 req/day, 8/min — bot uses ~96/day)

**Telegram Bot**
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` → follow prompts
3. Copy the **bot token** (looks like `123456:ABC-DEF...`)

**Get your Chat ID**
1. Message your new bot anything
2. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id": YOUR_CHAT_ID}` in the JSON
4. Copy that number (can also be a group/channel ID)

---

### Step 2 — Set Up Locally

```bash
# Clone your repo
git clone https://github.com/YOUR_USERNAME/xauusd-signal-bot
cd xauusd-signal-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your actual keys

# Run locally
python bot.py
```

---

### Step 3 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit — XAUUSD Signal Bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/xauusd-signal-bot.git
git push -u origin main
```

> ⚠️ **Never commit `.env`** — it's in `.gitignore` already.

---

### Step 4 — Deploy to Render (Free 24/7 Hosting)

1. Go to [render.com](https://render.com) and sign up
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — click **Apply**
5. Add environment variables under **Environment**:
   - `TWELVE_API_KEY` → your TwelveData key
   - `TELEGRAM_TOKEN` → your bot token
   - `TELEGRAM_CHAT_ID` → your chat ID
6. Click **Deploy**

**Keep-alive (prevent Render free tier sleep):**
- Go to [cron-job.org](https://cron-job.org) (free)
- Create a cron job hitting `https://your-app.onrender.com/health` every 5 minutes
- This keeps the bot awake 24/7 on the free tier

---

## 📁 File Structure

```
xauusd-signal-bot/
├── bot.py              ← Main bot (all logic here)
├── requirements.txt    ← Python dependencies
├── render.yaml         ← Render deployment config
├── .env.example        ← Environment variable template
├── .gitignore          ← Keeps .env out of git
└── README.md           ← This file
```

---

## ⚙️ Customisation

All key settings are at the top of `bot.py`:

```python
INTERVAL    = "15min"   # Change to "1h" for hourly signals
CANDLES     = 100       # Lookback window
THRESHOLD   = 6         # Min score to fire (6-7 recommended)
ATR_MULT    = 1.2       # SL tightness (higher = wider SL)
TP1_RATIO   = 1.5       # TP1 distance as multiple of SL
TP2_RATIO   = 3.0       # TP2 distance as multiple of SL
```

---

## ⚠️ Disclaimer

This bot is for **educational purposes only**. Trading gold (XAU/USD) carries significant risk. Past signal performance does not guarantee future results. Always apply your own analysis and risk management. Never risk more than you can afford to lose.
