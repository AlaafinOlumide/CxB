import os
import time
import threading
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
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", 3))

RISK_PER_TRADE_USD = float(os.getenv("RISK_PER_TRADE_USD", 100))
MAX_LOT_SIZE = float(os.getenv("MAX_LOT_SIZE", 0.50))

state = {
    "last_signal_time": None,
    "last_candle_time": None,
    "signals_today": 0,
    "current_day": datetime.now(timezone.utc).date(),
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
    response = requests.post(url, json=payload, timeout=20)
    return response.json()


def get_data(interval):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY,
    }

    response = requests.get(url, params=params, timeout=20)
    data = response.json()

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
    df["bb_upper"] = df["bb_mid"] + (2 * df["bb_std"])
    df["bb_lower"] = df["bb_mid"] - (2 * df["bb_std"])

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


def grade_from_score(score):
    if score >= 6:
        return "A+"
    if score == 5:
        return "A-"
    if score == 4:
        return "B+"
    if score == 3:
        return "B"
    return "NO TRADE"


def quality_from_score(score):
    if score >= 5:
        return "EXECUTION GRADE"
    if score == 4:
        return "WATCH CLOSELY"
    return "WAIT"


def lot_size_from_score(score, stop_distance):
    if score >= 6:
        base_lot = 0.35
    elif score == 5:
        base_lot = 0.25
    elif score == 4:
        base_lot = 0.15
    else:
        base_lot = 0.00

    if stop_distance <= 0:
        return 0.00

    risk_based_lot = RISK_PER_TRADE_USD / (stop_distance * 100)
    final_lot = min(base_lot, risk_based_lot, MAX_LOT_SIZE)

    return round(final_lot, 2)


def not_chasing_extreme(side, candle):
    if side == "BUY":
        return candle["close"] < candle["bb_upper"]
    if side == "SELL":
        return candle["close"] > candle["bb_lower"]
    return False


def analyse():
    m5 = indicators(get_data("5min"))
    m15 = indicators(get_data("15min"))

    c5 = m5.iloc[-2]
    p5 = m5.iloc[-3]
    c15 = m15.iloc[-2]

    price = round(float(c5["close"]), 2)

    buy_score = 0
    sell_score = 0
    buy_points = []
    sell_points = []

    if c15["close"] > c15["bb_mid"] or c15["rsi"] >= 50:
        buy_score += 1
        buy_points.append("M15 bullish/recovering")

    if c15["close"] < c15["bb_mid"] or c15["rsi"] <= 50:
        sell_score += 1
        sell_points.append("M15 bearish/weakening")

    if c5["close"] > c5["bb_mid"]:
        buy_score += 1
        buy_points.append("M5 closed above Bollinger midline")

    if c5["close"] < c5["bb_mid"]:
        sell_score += 1
        sell_points.append("M5 closed below Bollinger midline")

    if c5["rsi"] > 50:
        buy_score += 1
        buy_points.append(f"M5 RSI {c5['rsi']:.2f} above 50 and rising")

    if c5["rsi"] < 50:
        sell_score += 1
        sell_points.append(f"M5 RSI {c5['rsi']:.2f} below 50 and falling")

    if p5["stoch_k"] < p5["stoch_d"] and c5["stoch_k"] > c5["stoch_d"]:
        buy_score += 1
        buy_points.append("Stochastic bullish cross confirmed")

    if p5["stoch_k"] > p5["stoch_d"] and c5["stoch_k"] < c5["stoch_d"]:
        sell_score += 1
        sell_points.append("Stochastic bearish cross confirmed")

    if c5["close"] > c5["open"]:
        buy_score += 1
        buy_points.append("Bullish candle close confirmed")

    if c5["close"] < c5["open"]:
        sell_score += 1
        sell_points.append("Bearish candle close confirmed")

    if not_chasing_extreme("BUY", c5):
        buy_score += 1
        buy_points.append("Not chasing upper Bollinger Band")

    if not_chasing_extreme("SELL", c5):
        sell_score += 1
        sell_points.append("Not chasing lower Bollinger Band")

    if buy_score > sell_score:
        side = "BUY"
        score = buy_score
        key_points = buy_points
    elif sell_score > buy_score:
        side = "SELL"
        score = sell_score
        key_points = sell_points
    else:
        side = "WAIT"
        score = max(buy_score, sell_score)
        key_points = [
            "M5 and M15 are mixed",
            "No clean directional edge",
            "Waiting for stronger confirmation",
        ]

    if score < MIN_SIGNAL_SCORE:
        side = "WAIT"

    grade = grade_from_score(score)
    quality = quality_from_score(score)

    if side == "BUY":
        emoji = "🟢"
        entry_low = price
        entry_high = round(price + 2, 2)
        sl = round(min(float(c5["low"]), float(c5["bb_mid"])) - 2, 2)
        stop_distance = entry_low - sl
        tp1 = round(entry_low + stop_distance, 2)
        tp2 = round(entry_low + (stop_distance * 1.8), 2)
        entry_text = f"{entry_low} - {entry_high} after breakout/bullish close"
        invalidation = f"Bias fails if M5 closes below {sl} or RSI drops back below 50."
        why = "M5 trigger is bullish and M15 is not fighting the trade. Price remains above structure with momentum confirming continuation."

    elif side == "SELL":
        emoji = "🔴"
        entry_low = price
        entry_high = round(price + 2, 2)
        sl = round(max(float(c5["high"]), float(c5["bb_mid"])) + 2, 2)
        stop_distance = sl - entry_low
        tp1 = round(entry_low - stop_distance, 2)
        tp2 = round(entry_low - (stop_distance * 1.8), 2)
        entry_text = f"{entry_low} - {entry_high} after rejection/bearish close"
        invalidation = f"Bias fails if M5 closes above {sl} or RSI reclaims 50."
        why = "M5 trigger is bearish and M15 is not fighting the trade. Price remains below structure with momentum confirming continuation."

    else:
        emoji = "🟡"
        entry_text = "No trade"
        tp1 = "N/A"
        tp2 = "N/A"
        sl = "N/A"
        stop_distance = 0
        invalidation = "A confirmed M5 breakout or breakdown with M15 alignment."
        why = "M5 and M15 are not sufficiently aligned. Current structure lacks enough confirmation for execution."

    lot = lot_size_from_score(score, stop_distance)
    lot_text = "No Trade" if side == "WAIT" or lot == 0 else f"{lot:.2f} lot"

    message = f"""{emoji} XAUUSD SIGNAL

Best Scenario
{side} ({grade} | Score {score}/6)

Quality
{quality}

Entry
{entry_text}

Take Profit
TP1 {tp1} | TP2 {tp2}

Stop Loss
{sl}

Best Lot Size
{lot_text}

Key Points
""" + "\n".join([f"• {point}" for point in key_points]) + f"""

Why this bias
{why}

What can invalidate this bias
{invalidation}
"""

    return {
        "side": side,
        "score": score,
        "quality": quality,
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
        minutes_since_last = (now - state["last_signal_time"]).total_seconds() / 60
        if minutes_since_last < SIGNAL_COOLDOWN_MINUTES:
            return False

    return True


def run_once():
    signal = analyse()

    if should_send(signal):
        send_telegram(signal["message"])
        state["last_signal_time"] = datetime.now(timezone.utc)
        state["last_candle_time"] = signal["candle_time"]
        state["signals_today"] += 1
        return signal

    return {
        "status": "WAIT",
        "reason": "No valid signal, duplicate candle, cooldown active, or daily signal cap reached",
    }


def bot_loop():
    time.sleep(10)

    while True:
        try:
            result = run_once()
            print(result, flush=True)
        except Exception as error:
            print(f"Bot error: {error}", flush=True)

        time.sleep(POLL_INTERVAL_SECONDS)


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "symbol": SYMBOL,
        "signals_today": state["signals_today"],
        "max_signals_per_day": MAX_SIGNALS_PER_DAY,
        "cooldown_minutes": SIGNAL_COOLDOWN_MINUTES,
        "min_signal_score": MIN_SIGNAL_SCORE,
    })


@app.route("/run-once")
def manual_run():
    return jsonify(run_once())


@app.route("/test-telegram")
def test_telegram():
    result = send_telegram("✅ XAUUSD bot test message received.")
    return jsonify(result)


threading.Thread(target=bot_loop, daemon=True).start()