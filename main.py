import os
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from flask import Flask, jsonify

app = Flask(__name__)

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = os.getenv("SYMBOL", "XAU/USD")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 300))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", 20))
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", 4))

SCOUT_TIMEZONE = os.getenv("SCOUT_TIMEZONE", "Europe/London")

SCOUT_WINDOWS = [
    ("23:00", "03:00"),
    ("07:00", "10:00"),
    ("13:30", "16:00"),
]

state = {
    "last_signal_time": None,
    "last_candle_time": None,
}


def is_weekend_sleep():
    now = datetime.now(ZoneInfo(SCOUT_TIMEZONE))
    weekday = now.weekday()
    current_time = now.time()

    if weekday == 4 and current_time >= datetime.strptime("22:00", "%H:%M").time():
        return True

    if weekday == 5:
        return True

    if weekday == 6 and current_time < datetime.strptime("22:00", "%H:%M").time():
        return True

    return False


def is_within_scouting_time():
    now = datetime.now(ZoneInfo(SCOUT_TIMEZONE)).time()

    for start_str, end_str in SCOUT_WINDOWS:
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()

        if start < end:
            if start <= now <= end:
                return True
        else:
            if now >= start or now <= end:
                return True

    return False


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

    # ATR
    df["prev_close"] = df["close"].shift(1)
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["prev_close"]).abs()
    df["tr3"] = (df["low"] - df["prev_close"]).abs()
    df["true_range"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["true_range"].rolling(14).mean()

    # ADX
    df["up_move"] = df["high"] - df["high"].shift(1)
    df["down_move"] = df["low"].shift(1) - df["low"]

    df["+dm"] = df.apply(
        lambda row: row["up_move"] if row["up_move"] > row["down_move"] and row["up_move"] > 0 else 0,
        axis=1,
    )

    df["-dm"] = df.apply(
        lambda row: row["down_move"] if row["down_move"] > row["up_move"] and row["down_move"] > 0 else 0,
        axis=1,
    )

    df["+di"] = 100 * (df["+dm"].rolling(14).mean() / df["atr"])
    df["-di"] = 100 * (df["-dm"].rolling(14).mean() / df["atr"])
    df["dx"] = 100 * ((df["+di"] - df["-di"]).abs() / (df["+di"] + df["-di"]))
    df["adx"] = df["dx"].rolling(14).mean()

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


def lot_size_from_score(score):
    if score >= 6:
        return 0.50
    elif score == 5:
        return 0.25
    elif score == 4:
        return 0.15
    else:
        return 0.00


def h1_bias(candle):
    if candle["close"] > candle["bb_mid"] and candle["rsi"] >= 55:
        return "BUY"

    if candle["close"] < candle["bb_mid"] and candle["rsi"] <= 45:
        return "SELL"

    return "NEUTRAL"


def momentum_strength(candle):
    adx = candle["adx"]

    if adx >= 35:
        return "Very Strong"
    elif adx >= 25:
        return "Strong"
    elif adx >= 20:
        return "Medium"
    else:
        return "Weak"


def exhaustion_risk(side, candle, prev_candle):
    adx = candle["adx"]
    rsi = candle["rsi"]
    atr_now = candle["atr"]
    atr_prev = prev_candle["atr"]

    if side == "BUY":
        if rsi >= 70 and candle["close"] >= candle["bb_upper"]:
            return "High"
        if adx >= 35 and rsi >= 65:
            return "Medium"
        if atr_now < atr_prev and rsi >= 65:
            return "Medium"
        return "Low"

    if side == "SELL":
        if rsi <= 30 and candle["close"] <= candle["bb_lower"]:
            return "High"
        if adx >= 35 and rsi <= 35:
            return "Medium"
        if atr_now < atr_prev and rsi <= 35:
            return "Medium"
        return "Low"

    return "N/A"


def not_chasing_extreme(side, candle):
    if side == "BUY":
        return candle["close"] < candle["bb_upper"]

    if side == "SELL":
        return candle["close"] > candle["bb_lower"]

    return False


def analyse():
    h1 = indicators(get_data("1h"))
    m15 = indicators(get_data("15min"))
    m5 = indicators(get_data("5min"))

    c1 = h1.iloc[-2]
    c15 = m15.iloc[-2]
    c5 = m5.iloc[-2]
    p5 = m5.iloc[-3]

    price = round(float(c5["close"]), 2)
    higher_tf_bias = h1_bias(c1)

    buy_score = 0
    sell_score = 0
    buy_points = []
    sell_points = []

    if higher_tf_bias == "BUY":
        buy_score += 1
        buy_points.append("H1 bullish: buyers control higher timeframe")
        sell_points.append("H1 bullish: selling is blocked unless reversal confirms")

    elif higher_tf_bias == "SELL":
        sell_score += 1
        sell_points.append("H1 bearish: sellers control higher timeframe")
        buy_points.append("H1 bearish: buying is blocked unless reversal confirms")

    else:
        buy_points.append("H1 neutral")
        sell_points.append("H1 neutral")

    if c15["close"] > c15["bb_mid"] and c15["rsi"] >= 50:
        buy_score += 1
        buy_points.append("M15 bullish/recovering")

    if c15["close"] < c15["bb_mid"] and c15["rsi"] <= 50:
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

    # ADX momentum strength
    m_strength = momentum_strength(c5)

    if c5["adx"] >= 20:
        if buy_score > sell_score:
            buy_score += 1
            buy_points.append(f"ADX {c5['adx']:.2f}: momentum strength is {m_strength}")
        elif sell_score > buy_score:
            sell_score += 1
            sell_points.append(f"ADX {c5['adx']:.2f}: momentum strength is {m_strength}")
    else:
        buy_points.append(f"ADX {c5['adx']:.2f}: weak/choppy momentum")
        sell_points.append(f"ADX {c5['adx']:.2f}: weak/choppy momentum")

    # H1 protection
    if higher_tf_bias == "BUY":
        sell_score = 0

    if higher_tf_bias == "SELL":
        buy_score = 0

    if buy_score > sell_score and buy_score >= MIN_SIGNAL_SCORE:
        side = "BUY"
        score = min(buy_score, 6)
        key_points = buy_points

    elif sell_score > buy_score and sell_score >= MIN_SIGNAL_SCORE:
        side = "SELL"
        score = min(sell_score, 6)
        key_points = sell_points

    else:
        side = "WAIT"
        score = max(min(buy_score, 6), min(sell_score, 6))
        key_points = [
            f"H1 bias: {higher_tf_bias}",
            "M15 and M5 are not sufficiently aligned",
            "No clean confirmation yet",
            f"ADX {c5['adx']:.2f}: momentum strength is {m_strength}",
            "Avoid fighting the higher timeframe trend",
            "Wait for pullback and closed-candle confirmation",
        ]

    grade = grade_from_score(score)
    quality = quality_from_score(score)
    exhaustion = exhaustion_risk(side, c5, p5)

    if side == "BUY":
        emoji = "🟢"
        entry_low = price
        entry_high = round(price + 2, 2)
        sl = round(min(float(c5["low"]), float(c5["bb_mid"])) - 2, 2)
        stop_distance = entry_low - sl

        tp1 = round(entry_low + stop_distance, 2)
        tp2 = round(entry_low + (stop_distance * 1.8), 2)

        entry_text = f"{entry_low} - {entry_high} after breakout/bullish close"
        invalidation = f"Bias fails if M5 closes below {sl}, RSI drops below 50, ADX weakens below 20, or H1 loses bullish structure."
        why = "H1 supports buying, M15 is not fighting the trade, M5 confirms bullish momentum, and ADX shows whether the move has strength."

    elif side == "SELL":
        emoji = "🔴"
        entry_low = price
        entry_high = round(price + 2, 2)
        sl = round(max(float(c5["high"]), float(c5["bb_mid"])) + 2, 2)
        stop_distance = sl - entry_low

        tp1 = round(entry_low - stop_distance, 2)
        tp2 = round(entry_low - (stop_distance * 1.8), 2)

        entry_text = f"{entry_low} - {entry_high} after rejection/bearish close"
        invalidation = f"Bias fails if M5 closes above {sl}, RSI reclaims 50, ADX weakens below 20, or H1 turns bullish."
        why = "H1 supports selling, M15 is not fighting the trade, M5 confirms bearish momentum, and ADX shows whether the move has strength."

    else:
        emoji = "🟡"
        entry_text = "No trade"
        tp1 = "N/A"
        tp2 = "N/A"
        sl = "N/A"
        invalidation = "A confirmed H1, M15, and M5 alignment with closed-candle confirmation and stronger ADX momentum."
        why = "The bot is protecting the account by avoiding counter-trend entries, weak momentum, exhaustion zones, and emotional trades."

    lot = lot_size_from_score(score)
    lot_text = "No Trade" if side == "WAIT" or lot == 0 else f"{lot:.2f} lot"

    message = f"""{emoji} XAUUSD SIGNAL

Best Scenario
{side} ({grade} | Score {score}/6)

Quality
{quality}

Momentum Strength
{m_strength}

Exhaustion Risk
{exhaustion}

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
        "h1_bias": higher_tf_bias,
        "momentum_strength": m_strength,
        "exhaustion_risk": exhaustion,
        "message": message,
        "candle_time": str(c5["time"]),
    }


def should_send(signal):
    if not signal:
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
    if is_weekend_sleep():
        return {
            "status": "SLEEPING",
            "reason": "Weekend shutdown active",
            "sleep_window": "Friday 22:00 to Sunday 22:00",
            "timezone": SCOUT_TIMEZONE,
        }

    if not is_within_scouting_time():
        return {
            "status": "OFF_SESSION",
            "reason": "Outside high-probability trading windows",
            "scouting_windows": SCOUT_WINDOWS,
            "timezone": SCOUT_TIMEZONE,
        }

    signal = analyse()

    if should_send(signal):
        send_telegram(signal["message"])
        state["last_signal_time"] = datetime.now(timezone.utc)
        state["last_candle_time"] = signal["candle_time"]
        return signal

    return {
        "status": "WAIT",
        "reason": "No valid signal, duplicate candle, or cooldown active",
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
        "strategy": "H1 trend filter + M15 alignment + M5 confirmation + ADX/ATR momentum filter",
        "scout_timezone": SCOUT_TIMEZONE,
        "scout_windows": SCOUT_WINDOWS,
        "currently_scouting": is_within_scouting_time(),
        "weekend_sleep": "Friday 22:00 to Sunday 22:00",
        "sleeping_now": is_weekend_sleep(),
        "cooldown_minutes": SIGNAL_COOLDOWN_MINUTES,
        "min_signal_score": MIN_SIGNAL_SCORE,
        "lot_sizes": {
            "A+": "0.50 lot",
            "A-": "0.25 lot",
            "B+": "0.15 lot",
            "B/WAIT": "No Trade",
        },
    })


@app.route("/run-once")
def manual_run():
    return jsonify(run_once())


@app.route("/test-telegram")
def test_telegram():
    result = send_telegram("✅ XAUUSD bot test message received.")
    return jsonify(result)


threading.Thread(target=bot_loop, daemon=True).start()