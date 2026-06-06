# XAUUSD Twelve Data → Telegram Signal Bot

Signal-only bot for XAUUSD based on the rule set we built from your screenshots:

**Output format**
- Best Scenario
- Best Lot Size
- Entry
- Take Profit
- Stop Loss
- Key Points
- Why this bias
- What can invalidate this bias

It does **not** place trades. It sends Telegram alerts only.

## Strategy summary

Timeframes:
- M5 trigger
- M15 confirmation

Indicators:
- Bollinger Bands 20, 2
- RSI 14
- Stochastic 5,3,3

Core rules:
- BUY only when M5 and M15 align bullish.
- SELL only when M5 and M15 align bearish.
- If they conflict, WAIT.
- Avoid chasing price at upper/lower Bollinger Band extremes.
- Use prop-account friendly lot suggestions: 0.20 normal, 0.25 aggressive, 0.30 max.

## Twelve Data free-plan friendly polling

Default polling is every 300 seconds / 5 minutes.

Each cycle calls:
- M5 candles
- M15 candles

That is around 576 calls/day, designed to stay under the common Twelve Data free allowance of about 800/day.

## Telegram setup

1. Open Telegram and message `@BotFather`.
2. Create a bot with `/newbot`.
3. Copy the bot token.
4. Send a message to your bot, or add it to a channel/group.
5. Get your chat ID using a bot like `@userinfobot`, or by calling Telegram `getUpdates`.

## Render deployment

### Option A: Blueprint
1. Push this folder to GitHub.
2. In Render, choose **New → Blueprint**.
3. Select the repo.
4. Render reads `render.yaml`.
5. Add secrets:
   - `TWELVE_DATA_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

### Option B: Manual Web Service
1. New → Web Service.
2. Build command:
   ```bash
   pip install -r requirements.txt
   ```
3. Start command:
   ```bash
   python main.py
   ```
4. Add the same environment variables.

## Important Render note

For true 24/7, use a paid Render web service or worker.
Render free web services can spin down after inactivity, so the bot may stop polling while asleep.

If you still use the free web service, you can use an external uptime ping to call:

```text
https://your-service.onrender.com/health
```

every 10 minutes, but the more reliable solution is a paid always-on service.

## Environment variables

See `.env.example`.

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py
```

The web server runs on `/`, `/health`, and `/last`.

## Disclaimer

This is a signal tool, not financial advice. Test in paper mode first and respect Equity Edge drawdown rules.
