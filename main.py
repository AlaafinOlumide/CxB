import os, time, threading
from datetime import datetime, timezone
import requests
import pandas as pd
from flask import Flask, jsonify

app = Flask(__name__)

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 300))

MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", 15))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", 20))
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", 4))
MIN_RISK_REWARD = float(os.getenv("MIN_RISK_REWARD", 1.5))

state = {
    "last_signal_time": None,
    "last_signal_side": None,
    "signals_today": 0,
    "current_day": datetime.now(timezone.utc).date(),
    "last_candle_time": None,
}


def reset_daily_counter():
    today = datetime.now(timezone.utc).date()
    if state["current_day"] != today:
        state["current_day"] = today
        state["signals_today"] = 0


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=20)
    return r.json()


def get_data(interval):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY,
    }

    data = requests.get(url, params=params, timeout=20).json()

    if "values" not in data:
        raise Exception(f"Twelve Data error: {data}")

    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"datetime": "time"})
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time")

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)

    return df.reset_index(drop=True)


def indicators(df):
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    low14 = df["low"].rolling(14).min()
    high14 = df["high"].rolling(14).max()
    df["stoch_k"] = 100 * ((df["close"] - low14) / (high14 - low14))
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    return df.dropna().reset_index(drop=True)


def classify_signal(score):
    if score >= 7:
        return "🔥 A+ SIGNAL", "Execution grade", "0.30–0.50 lots"
    if score >= 6:
        return "⚡ A SIGNAL", "Tradable", "0.20–0.30 lots"
    return "📋 B WATCHLIST", "Wait for stronger confirmation", "0.10–0.20 lots"


def analyse():
    m5 = indicators(get_data("5min"))
    m15 = indicators(get_data("15min"))

    c5 = m5.iloc[-2]      # closed candle
    p5 = m5.iloc[-3]
    c15 = m15.iloc[-2]

    price = c5["close"]
    score_buy = 0
    score_sell = 0
    key_buy = []
    key_sell = []

    # M15 alignment
    if c15["close"] > c15["bb_mid"] and c15["rsi"] > 50:
        score_buy += 2
        key_buy.append("M15 bullish alignment")
    if c15["close"] < c15["bb_mid"] and c15["rsi"] < 50:
        score_sell += 2
        key_sell.append("M15 bearish alignment")

    # M5 Bollinger position
    if c5["close"] > c5["bb_mid"]:
        score_buy += 1
        key_buy.append("M5 price above BB midline")
    if c5["close"] < c5["bb_mid"]:
        score_sell += 1
        key_sell.append("M5 price below BB midline")

    # RSI
    if c5["rsi"] > 50:
        score_buy += 1
        key_buy.append("M5 RSI above 50")
    if c5["rsi"] < 50:
        score_sell += 1
        key_sell.append("M5 RSI below 50")

    # Stochastic cross
    if p5["stoch_k"] < p5["stoch_d"] and c5["stoch_k"] > c5["stoch_d"]:
        score_buy += 2
        key_buy.append("Stochastic bullish cross")
    if p5["stoch_k"] > p5["stoch_d"] and c5["stoch_k"] < c5["stoch_d"]:
        score_sell += 2
        key_sell.append("Stochastic bearish cross")

    # Candle confirmation
    if c5["close"] > c5["open"]:
        score_buy += 1
        key_buy.append("Bullish closed candle")
    if c5["close"] < c5["open"]:
        score_sell += 1
        key_sell.append("Bearish closed candle")

    if score_buy > score_sell and score_buy >= MIN_SIGNAL_SCORE:
        side = "BUY"
        score = score_buy
        key_points = key_buy
        entry = round(price, 2)
        sl = round(min(c5["low"], c5["bb_mid"]) - 2, 2)
        risk = entry - sl
        tp1 = round(entry + risk * 1.0, 2)
        tp2 = round(entry + risk * 1.5, 2)
        tp3 = round(entry + risk * 2.0, 2)
        invalidation = f"Bias fails if M5 closes below {sl} or RSI drops back below 50."

    elif score_sell > score_buy and score_sell >= MIN_SIGNAL_SCORE:
        side = "SELL"
        score = score_sell
        key_points = key_sell
        entry = round(price, 2)
        sl = round(max(c5["high"], c5["bb_mid"]) + 2, 2)
        risk = sl - entry
        tp1 = round(entry - risk * 1.0, 2)
        tp2 = round(entry - risk * 1.5, 2)
        tp3 = round(entry - risk * 2.0, 2)
        invalidation = f"Bias fails if M5 closes above {sl} or RSI reclaims 50."

    else:
        return None

    if risk <= 0:
        return None

    rr = abs(tp3 - entry) / risk
    if rr < MIN_RISK_REWARD:
        return None

    label, quality, lot_size = classify_signal(score)

    message = f"""
{label}

<b>XAUUSD SIGNAL</b>

<b>Best Scenario:</b> {side}
<b>Quality:</b> {quality}
<b>Score:</b> {score}/8

<b>Entry:</b>
{entry}

<b>Take Profit:</b>
TP1: {tp1}
TP2: {tp2}
TP3: {tp3}

<b>Stop Loss:</b>
{sl}

<b>Suggested Lot Size:</b>
{lot_size}

<b>Key Points:</b>
""" + "\n".join([f"• {x}" for x in key_points]) + f"""

<b>Why This Bias:</b>
{side} bias is supported by M15 structure, M5 confirmation, RSI condition, stochastic direction, and closed candle momentum.

<b>What Can Invalidate This Bias:</b>
{invalidation}

<b>Prop Rule:</b>
A+ = executable.
A = tradable with caution.
B = watchlist only unless manually confirmed.
"""

    return {
        "side": side,
        "score": score,
        "message": message,
        "candle_time": str(c5["time"]),
    }


def should_send(signal):
    reset_daily_counter()

    if not signal:
        return False

    if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        return False

    if signal["candle_time"] == state["last_candle_time"]:
        return False

    now = datetime.now(timezone.utc)

    if state["last_signal_time"]:
        minutes = (now - state["last_signal_time"]).total_seconds() / 60
        if minutes < SIGNAL_COOLDOWN_MINUTES:
            return False

    return True


def run_once():
    signal = analyse()

    if should_send(signal):
        send_telegram(signal["message"])
        state["last_signal_time"] = datetime.now(timezone.utc)
        state["last_signal_side"] = signal["side"]
        state["signals_today"] += 1
        state["last_candle_time"] = signal["candle_time"]
        return signal

    return {"status": "WAIT", "reason": "No valid signal or cooldown active"}


def bot_loop():
    time.sleep(10)
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Bot error: {e}", flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "symbol": SYMBOL,
        "signals_today": state["signals_today"],
        "max_signals_per_day": MAX_SIGNALS_PER_DAY,
    })


@app.route("/run-once")
def manual_run():
    return jsonify(run_once())


@app.route("/test-telegram")
def test_telegram():
    result = send_telegram("✅ XAUUSD bot test message received.")
    return jsonify(result)


threading.Thread(target=bot_loop, daemon=True).start()