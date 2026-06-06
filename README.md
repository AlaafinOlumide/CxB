# XAUUSD Twelve Data → Telegram Signal Bot

Signal-only bot for XAUUSD. It does **not** place trades.

## Improved version

This version improves the previous bot by:

- Starting the background signal loop correctly under `gunicorn main:app` on Render.
- Using only **closed M5 and M15 candles**, not forming candles.
- Sending fewer Telegram alerts using score-based filtering.
- Using your exact signal format:
  - Best Scenario
  - Entry
  - Take Profit
  - Stop Loss
  - Best Lot Size
  - Key Points
  - Why this bias
  - What can invalidate this bias
- Adding `/test-telegram` and `/run-once` routes.
- Calculating lot size from stop distance and max risk per trade.

## Strategy

Timeframes:
- M5 trigger
- M15 confirmation

Indicators:
- Bollinger Bands 20, 2
- RSI 14
- Stochastic 5,3,3

Scoring:
- BUY or SELL needs at least 4/6 alignment.
- 5/6 = A-
- 6/6 = A
- Anything weaker = WAIT.

Minimum alignment:
- M15 must not fight the trade.
- Candle close confirmation required.
- RSI must support direction.
- Stochastic must support direction.
- Avoid chasing upper/lower Bollinger Band extremes.

## Render setup

Root Directory:
```bash
xauusd_signal_bot
```

Build Command:
```bash
pip install -r requirements.txt
```

Start Command:
```bash
gunicorn main:app
```

Python:
```bash
3.12.8
```

## Required environment variables

```bash
TWELVE_DATA_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Recommended environment variables

```bash
SYMBOL=XAU/USD
POLL_SECONDS=300
OUTPUTSIZE=120
ACCOUNT_SIZE=50000
DAILY_TARGET_MIN=250
DAILY_TARGET_MAX=350
MAX_RISK_PER_TRADE=120
NORMAL_LOT=0.15
AGGRESSIVE_LOT=0.25
MAX_LOT=0.30
SIGNAL_COOLDOWN_MINUTES=30
SEND_WAIT_SIGNALS=false
SEND_STARTUP_MESSAGE=true
PYTHON_VERSION=3.12.8
```

## Useful routes

```text
/health
/last
/run-once
/test-telegram
```

Use `/test-telegram` first to confirm Telegram connection.

## Important

This is a signal tool, not financial advice. Test on demo/paper first. Respect prop-firm drawdown rules.
