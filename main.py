import os
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# ENVIRONMENT SETTINGS
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "20"))
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "4"))
MIN_ADX = float(os.getenv("MIN_ADX", "18"))
SCOUT_TIMEZONE = os.getenv("SCOUT_TIMEZONE", "Europe/London")

# Set SEND_WATCHLIST_SIGNALS=false if you only want A-/A+ alerts.
SEND_WATCHLIST_SIGNALS = (
    os.getenv("SEND_WATCHLIST_SIGNALS", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)

# UK scouting windows. Overnight windows are supported.
SCOUT_WINDOWS = [
    ("23:00", "03:00"),
    ("07:00", "10:00"),
    ("13:30", "16:00"),
]

state = {
    "last_signal_time": None,
    "last_candle_time": None,
    "last_signal_side": None,
    "last_result": None,
}

run_lock = threading.Lock()


# ============================================================
# TIME AND SESSION CONTROL
# ============================================================


def london_now():
    return datetime.now(ZoneInfo(SCOUT_TIMEZONE))


def is_weekend_sleep():
    """Friday 22:00 UK time until Sunday 22:00 UK time."""
    now = london_now()
    weekday = now.weekday()
    current_time = now.time()
    boundary = datetime.strptime("22:00", "%H:%M").time()

    if weekday == 4 and current_time >= boundary:
        return True
    if weekday == 5:
        return True
    if weekday == 6 and current_time < boundary:
        return True
    return False


def is_within_scouting_time():
    """Return True when current UK time is inside an approved window."""
    current_time = london_now().time()

    for start_text, end_text in SCOUT_WINDOWS:
        start = datetime.strptime(start_text, "%H:%M").time()
        end = datetime.strptime(end_text, "%H:%M").time()

        if start < end:
            if start <= current_time <= end:
                return True
        else:
            # Overnight window, for example 23:00 to 03:00.
            if current_time >= start or current_time <= end:
                return True

    return False


# ============================================================
# TELEGRAM
# ============================================================


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")

    return result


# ============================================================
# TWELVE DATA
# ============================================================


def get_data(interval):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY")

    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": 180,
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {interval}: {data}")

    df = pd.DataFrame(data["values"])
    required = {"datetime", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing data columns for {interval}: {missing}")

    df = df.rename(columns={"datetime": "time"})
    df["time"] = pd.to_datetime(df["time"], errors="coerce")

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = (
        df.dropna(subset=["time", "open", "high", "low", "close"])
        .sort_values("time")
        .drop_duplicates(subset=["time"], keep="last")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# INDICATORS
# ============================================================


def calculate_indicators(df):
    df = df.copy()

    # Bollinger Bands
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + (2 * df["bb_std"])
    df["bb_lower"] = df["bb_mid"] - (2 * df["bb_std"])
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]

    # RSI (Wilder-style smoothing)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    average_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + relative_strength))

    # Stochastic
    lowest_low = df["low"].rolling(14).min()
    highest_high = df["high"].rolling(14).max()
    stochastic_range = (highest_high - lowest_low).replace(0, float("nan"))
    df["stoch_k"] = 100 * ((df["close"] - lowest_low) / stochastic_range)
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # ATR
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["true_range"] = true_range
    df["atr"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # ADX and directional indicators
    up_move = df["high"] - df["high"].shift(1)
    down_move = df["low"].shift(1) - df["low"]

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    smoothed_plus_dm = plus_dm.ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()
    smoothed_minus_dm = minus_dm.ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()

    atr_safe = df["atr"].replace(0, float("nan"))
    df["plus_di"] = 100 * smoothed_plus_dm / atr_safe
    df["minus_di"] = 100 * smoothed_minus_dm / atr_safe

    di_sum = (df["plus_di"] + df["minus_di"]).replace(0, float("nan"))
    df["dx"] = 100 * ((df["plus_di"] - df["minus_di"]).abs() / di_sum)
    df["adx"] = df["dx"].ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()

    df = df.replace([float("inf"), -float("inf")], float("nan"))
    return df.dropna().reset_index(drop=True)


# ============================================================
# GRADING AND LOT SIZE
# ============================================================


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
        return "WAIT FOR ENTRY CONFIRMATION"
    return "NO TRADE"


def lot_size_from_score(score):
    if score >= 6:
        return 0.50
    if score == 5:
        return 0.25
    if score == 4:
        return 0.15
    return 0.00


# ============================================================
# TREND AND MOMENTUM
# ============================================================


def h1_bias(candle):
    bullish = (
        candle["close"] > candle["bb_mid"]
        and candle["rsi"] >= 52
        and candle["plus_di"] > candle["minus_di"]
    )
    bearish = (
        candle["close"] < candle["bb_mid"]
        and candle["rsi"] <= 48
        and candle["minus_di"] > candle["plus_di"]
    )

    if bullish:
        return "BUY"
    if bearish:
        return "SELL"
    return "NEUTRAL"


def momentum_strength(candle):
    adx = float(candle["adx"])
    if adx >= 35:
        return "Very Strong"
    if adx >= 25:
        return "Strong"
    if adx >= 18:
        return "Developing"
    return "Weak"


def atr_is_healthy(current, previous):
    if previous["atr"] <= 0:
        return False
    return (current["atr"] / previous["atr"]) >= 0.88


def momentum_is_valid(side, current, previous):
    # Allow ADX to cool modestly while the directional trend remains intact.
    adx_acceptable = current["adx"] >= MIN_ADX
    adx_not_collapsing = current["adx"] >= previous["adx"] * 0.94

    if side == "BUY":
        direction_valid = current["plus_di"] > current["minus_di"]
    else:
        direction_valid = current["minus_di"] > current["plus_di"]

    return adx_acceptable and adx_not_collapsing and direction_valid


# ============================================================
# CANDLE PATTERNS
# ============================================================


def candle_body(candle):
    return abs(candle["close"] - candle["open"])


def candle_range(candle):
    return max(candle["high"] - candle["low"], 0.0001)


def bullish_engulfing(current, previous):
    return (
        current["close"] > current["open"]
        and previous["close"] < previous["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(current, previous):
    return (
        current["close"] < current["open"]
        and previous["close"] > previous["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_rejection(candle):
    body = max(candle_body(candle), 0.0001)
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    return (
        candle["close"] > candle["open"]
        and lower_wick >= body * 1.2
        and lower_wick > upper_wick
    )


def bearish_rejection(candle):
    body = max(candle_body(candle), 0.0001)
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    return (
        candle["close"] < candle["open"]
        and upper_wick >= body * 1.2
        and upper_wick > lower_wick
    )


def bullish_breakout_retest(current, previous, older):
    breakout_level = max(older["high"], older["bb_mid"])
    tolerance = max(float(current["atr"]) * 0.15, 0.25)
    return (
        previous["close"] > breakout_level
        and current["low"] <= breakout_level + tolerance
        and current["close"] > breakout_level
        and current["close"] > current["open"]
    )


def bearish_breakout_retest(current, previous, older):
    breakout_level = min(older["low"], older["bb_mid"])
    tolerance = max(float(current["atr"]) * 0.15, 0.25)
    return (
        previous["close"] < breakout_level
        and current["high"] >= breakout_level - tolerance
        and current["close"] < breakout_level
        and current["close"] < current["open"]
    )


def strong_directional_candle(side, candle):
    body_ratio = candle_body(candle) / candle_range(candle)

    if side == "BUY":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.55
            and candle["close"] > candle["bb_mid"]
        )

    if side == "SELL":
        return (
            candle["close"] < candle["open"]
            and body_ratio >= 0.55
            and candle["close"] < candle["bb_mid"]
        )

    return False


def confirmation_type(side, current, previous, older):
    if side == "BUY":
        if bullish_engulfing(current, previous):
            return "Bullish engulfing"
        if bullish_rejection(current):
            return "Bullish rejection"
        if bullish_breakout_retest(current, previous, older):
            return "Breakout and retest"
        if strong_directional_candle("BUY", current):
            return "Strong bullish candle close"

    if side == "SELL":
        if bearish_engulfing(current, previous):
            return "Bearish engulfing"
        if bearish_rejection(current):
            return "Bearish rejection"
        if bearish_breakout_retest(current, previous, older):
            return "Breakdown and retest"
        if strong_directional_candle("SELL", current):
            return "Strong bearish candle close"

    return None


# ============================================================
# EXHAUSTION AND EXTENSION FILTERS
# ============================================================


def exhaustion_risk(side, current, previous):
    atr = float(current["atr"])
    if atr <= 0:
        return "High"

    current_range = candle_range(current)
    large_expansion = current_range >= atr * 1.7
    atr_falling = current["atr"] < previous["atr"] * 0.86

    if side == "BUY":
        if (
            current["rsi"] >= 74
            and current["close"] >= current["bb_upper"]
            and large_expansion
        ):
            return "High"
        if current["rsi"] >= 69 and (
            current["close"] >= current["bb_upper"] or atr_falling
        ):
            return "Medium"

    if side == "SELL":
        if (
            current["rsi"] <= 26
            and current["close"] <= current["bb_lower"]
            and large_expansion
        ):
            return "High"
        if current["rsi"] <= 31 and (
            current["close"] <= current["bb_lower"] or atr_falling
        ):
            return "Medium"

    return "Low"


def price_is_not_extremely_extended(side, candle):
    atr = float(candle["atr"])
    if atr <= 0:
        return False

    if side == "BUY":
        distance = candle["close"] - candle["bb_mid"]
    else:
        distance = candle["bb_mid"] - candle["close"]

    # Hard block only when the entry is severely extended.
    return distance <= atr * 1.80


# ============================================================
# SIGNAL BUILDING
# ============================================================


def build_trade_levels(side, m5_current, confirmation):
    price = round(float(m5_current["close"]), 2)
    atr = float(m5_current["atr"])

    entry_buffer = max(round(atr * 0.18, 2), 0.40)
    stop_buffer = max(round(atr * 0.22, 2), 0.65)
    trigger_text = confirmation.lower() if confirmation else "entry-zone confirmation"

    if side == "BUY":
        entry_low = price
        entry_high = round(price + entry_buffer, 2)
        structural_stop = min(
            float(m5_current["low"]),
            float(m5_current["bb_mid"]),
        )
        stop_loss = round(structural_stop - stop_buffer, 2)
        risk = entry_low - stop_loss

        if risk <= 0:
            raise RuntimeError("Invalid BUY risk calculation")

        tp1 = round(entry_low + risk, 2)
        tp2 = round(entry_low + (risk * 1.7), 2)
        entry_text = (
            f"{entry_low:.2f} - {entry_high:.2f} after {trigger_text}"
        )

    else:
        entry_high = price
        entry_low = round(price - entry_buffer, 2)
        structural_stop = max(
            float(m5_current["high"]),
            float(m5_current["bb_mid"]),
        )
        stop_loss = round(structural_stop + stop_buffer, 2)
        risk = stop_loss - entry_high

        if risk <= 0:
            raise RuntimeError("Invalid SELL risk calculation")

        tp1 = round(entry_high - risk, 2)
        tp2 = round(entry_high - (risk * 1.7), 2)
        entry_text = (
            f"{entry_low:.2f} - {entry_high:.2f} after {trigger_text}"
        )

    return {
        "entry": entry_text,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
    }


def analyse():
    h1 = calculate_indicators(get_data("1h"))
    m15 = calculate_indicators(get_data("15min"))
    m5 = calculate_indicators(get_data("5min"))

    if len(h1) < 4 or len(m15) < 4 or len(m5) < 5:
        raise RuntimeError("Insufficient indicator data")

    # Closed candles only.
    h1_current = h1.iloc[-2]
    m15_current = m15.iloc[-2]
    m5_current = m5.iloc[-2]
    m5_previous = m5.iloc[-3]
    m5_older = m5.iloc[-4]

    side = h1_bias(h1_current)
    candle_time = str(m5_current["time"])

    # H1 direction remains mandatory because it defines the trading side.
    if side == "NEUTRAL":
        return {
            "side": "WAIT",
            "reason": "H1 trend is neutral",
            "h1_rsi": round(float(h1_current["rsi"]), 2),
            "candle_time": candle_time,
        }

    conditions = []
    key_points = []
    failed_conditions = []

    # 1. H1 direction: mandatory and therefore always true here.
    conditions.append(True)
    if side == "BUY":
        key_points.append("H1 bullish: buyers control the higher timeframe")
    else:
        key_points.append("H1 bearish: sellers control the higher timeframe")

    # 2. M15 alignment. RSI is deliberately relaxed to avoid over-filtering.
    if side == "BUY":
        m15_condition = (
            m15_current["close"] > m15_current["bb_mid"]
            and m15_current["rsi"] >= 48
            and m15_current["plus_di"] > m15_current["minus_di"]
        )
    else:
        m15_condition = (
            m15_current["close"] < m15_current["bb_mid"]
            and m15_current["rsi"] <= 52
            and m15_current["minus_di"] > m15_current["plus_di"]
        )

    conditions.append(m15_condition)
    if m15_condition:
        key_points.append(f"M15 structure and DI align with the {side} bias")
    else:
        failed_conditions.append("M15 alignment")
        key_points.append("M15 alignment is incomplete")

    # 3. M5 Bollinger structure.
    if side == "BUY":
        m5_structure = m5_current["close"] > m5_current["bb_mid"]
    else:
        m5_structure = m5_current["close"] < m5_current["bb_mid"]

    conditions.append(m5_structure)
    if m5_structure:
        position = "above" if side == "BUY" else "below"
        key_points.append(f"M5 closed {position} the Bollinger midline")
    else:
        failed_conditions.append("M5 structure")
        key_points.append("M5 has not fully confirmed structure")

    # 4. M5 RSI alignment. Permit a small one-candle pause in momentum.
    if side == "BUY":
        rsi_condition = (
            m5_current["rsi"] >= 50
            and m5_current["rsi"] >= m5_previous["rsi"] - 1.5
        )
    else:
        rsi_condition = (
            m5_current["rsi"] <= 50
            and m5_current["rsi"] <= m5_previous["rsi"] + 1.5
        )

    conditions.append(rsi_condition)
    if rsi_condition:
        key_points.append(
            f"M5 RSI {m5_current['rsi']:.2f} supports the {side} direction"
        )
    else:
        failed_conditions.append("M5 RSI")
        key_points.append(
            f"M5 RSI {m5_current['rsi']:.2f} is not fully aligned"
        )

    # 5. ADX, directional movement and ATR.
    momentum_condition = (
        momentum_is_valid(side, m5_current, m5_previous)
        and atr_is_healthy(m5_current, m5_previous)
    )
    conditions.append(momentum_condition)

    strength = momentum_strength(m5_current)
    if momentum_condition:
        di_text = "+DI above -DI" if side == "BUY" else "-DI above +DI"
        key_points.append(
            f"ADX {m5_current['adx']:.2f} ({strength}); {di_text}"
        )
    else:
        failed_conditions.append("ADX/DI/ATR momentum")
        key_points.append(
            f"Momentum filter is incomplete: ADX {m5_current['adx']:.2f}"
        )

    # 6. Premium M5 candle pattern. This is now scored, not mandatory.
    confirmation = confirmation_type(
        side,
        m5_current,
        m5_previous,
        m5_older,
    )
    confirmation_condition = confirmation is not None
    conditions.append(confirmation_condition)

    if confirmation_condition:
        key_points.append(f"{confirmation} confirmed")
    else:
        failed_conditions.append("Premium M5 candle pattern")
        key_points.append(
            "No premium candle pattern; wait for reaction inside the entry zone"
        )

    # Hard safety filters remain outside the score.
    exhaustion = exhaustion_risk(side, m5_current, m5_previous)
    if exhaustion == "High":
        return {
            "side": "WAIT",
            "reason": "Momentum exhaustion risk is high",
            "h1_bias": side,
            "exhaustion_risk": exhaustion,
            "candle_time": candle_time,
        }

    if not price_is_not_extremely_extended(side, m5_current):
        return {
            "side": "WAIT",
            "reason": "Price is excessively extended from the M5 mean",
            "h1_bias": side,
            "candle_time": candle_time,
        }

    score = sum(1 for condition in conditions if condition)
    grade = grade_from_score(score)
    quality = quality_from_score(score)

    if score < MIN_SIGNAL_SCORE:
        return {
            "side": "WAIT",
            "reason": (
                f"Score {score}/6 is below the minimum "
                f"{MIN_SIGNAL_SCORE}/6"
            ),
            "h1_bias": side,
            "score": score,
            "failed_conditions": failed_conditions,
            "candle_time": candle_time,
        }

    # A+ must include a real confirmation candle because that is the sixth point.
    # A- can be valid with one missing factor. B+ is watchlist only.
    levels = build_trade_levels(side, m5_current, confirmation)
    lot = lot_size_from_score(score)
    emoji = "🟢" if side == "BUY" else "🔴"

    if quality == "EXECUTION GRADE":
        action_note = "Trade only after price reacts inside the stated entry zone."
    else:
        action_note = (
            "WATCHLIST ONLY — do not enter yet. Wait for a fresh M5 rejection, "
            "engulfing close, or breakout-retest in the entry zone."
        )

    if side == "BUY":
        why = (
            "H1 provides the bullish bias. The score reflects how well M15, "
            "M5 structure, RSI, momentum and candle confirmation support continuation."
        )
        invalidation = (
            f"Bias weakens if M5 closes below {levels['stop_loss']:.2f}, "
            "RSI loses 48, directional movement turns bearish, or H1 loses its midline."
        )
    else:
        why = (
            "H1 provides the bearish bias. The score reflects how well M15, "
            "M5 structure, RSI, momentum and candle confirmation support continuation."
        )
        invalidation = (
            f"Bias weakens if M5 closes above {levels['stop_loss']:.2f}, "
            "RSI reclaims 52, directional movement turns bullish, or H1 regains its midline."
        )

    failed_text = (
        "None"
        if not failed_conditions
        else ", ".join(failed_conditions)
    )

    message = (
        f"{emoji} XAUUSD SIGNAL\n\n"
        f"Best Scenario\n"
        f"{side} ({grade} | Score {score}/6)\n\n"
        f"Quality\n"
        f"{quality}\n\n"
        f"Action\n"
        f"{action_note}\n\n"
        f"Momentum Strength\n"
        f"{strength}\n\n"
        f"Exhaustion Risk\n"
        f"{exhaustion}\n\n"
        f"Entry\n"
        f"{levels['entry']}\n\n"
        f"Take Profit\n"
        f"TP1 {levels['tp1']:.2f} | TP2 {levels['tp2']:.2f}\n\n"
        f"Stop Loss\n"
        f"{levels['stop_loss']:.2f}\n\n"
        f"Best Lot Size\n"
        f"{lot:.2f} lot\n\n"
        f"Key Points\n"
        + "\n".join(f"• {point}" for point in key_points)
        + "\n\n"
        f"Missing Conditions\n"
        f"{failed_text}\n\n"
        f"Why this bias\n"
        f"{why}\n\n"
        f"What can invalidate this bias\n"
        f"{invalidation}"
    )

    return {
        "side": side,
        "score": score,
        "grade": grade,
        "quality": quality,
        "h1_bias": side,
        "momentum_strength": strength,
        "exhaustion_risk": exhaustion,
        "confirmation": confirmation,
        "failed_conditions": failed_conditions,
        "entry": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "lot_size": lot,
        "message": message,
        "candle_time": candle_time,
    }


# ============================================================
# SIGNAL DELIVERY CONTROL
# ============================================================


def should_send(signal):
    if not signal or signal.get("side") == "WAIT":
        return False

    score = int(signal.get("score", 0))
    if score < MIN_SIGNAL_SCORE:
        return False

    if score == 4 and not SEND_WATCHLIST_SIGNALS:
        return False

    candle_time = signal.get("candle_time")
    if candle_time == state["last_candle_time"]:
        return False

    now = datetime.now(timezone.utc)
    if state["last_signal_time"]:
        elapsed_minutes = (
            now - state["last_signal_time"]
        ).total_seconds() / 60
        if elapsed_minutes < SIGNAL_COOLDOWN_MINUTES:
            return False

    return True


def run_once():
    if not run_lock.acquire(blocking=False):
        return {
            "status": "BUSY",
            "reason": "An analysis cycle is already running",
        }

    try:
        if is_weekend_sleep():
            result = {
                "status": "SLEEPING",
                "reason": "Weekend shutdown active",
                "sleep_window": "Friday 22:00 to Sunday 22:00",
                "timezone": SCOUT_TIMEZONE,
            }
            state["last_result"] = result
            return result

        if not is_within_scouting_time():
            result = {
                "status": "OFF_SESSION",
                "reason": "Outside approved scouting windows",
                "scouting_windows": SCOUT_WINDOWS,
                "timezone": SCOUT_TIMEZONE,
            }
            state["last_result"] = result
            return result

        signal = analyse()

        if should_send(signal):
            send_telegram(signal["message"])
            state["last_signal_time"] = datetime.now(timezone.utc)
            state["last_candle_time"] = signal["candle_time"]
            state["last_signal_side"] = signal["side"]
            state["last_result"] = signal

            print(
                f"Signal sent: {signal['side']} {signal['grade']} "
                f"{signal['score']}/6",
                flush=True,
            )
            return signal

        result = {
            "status": "WAIT",
            "reason": signal.get(
                "reason",
                "Signal was blocked by score, duplicate-candle protection, "
                "watchlist settings, or cooldown",
            ),
            "analysis": signal,
        }
        state["last_result"] = result
        return result

    finally:
        run_lock.release()


# ============================================================
# BACKGROUND LOOP
# ============================================================


def bot_loop():
    time.sleep(10)

    while True:
        try:
            result = run_once()
            print(result, flush=True)
        except Exception as error:
            print(f"Bot error: {error}", flush=True)

        time.sleep(POLL_INTERVAL_SECONDS)


# ============================================================
# FLASK ROUTES
# ============================================================


@app.route("/")
def home():
    now = london_now()

    return jsonify(
        {
            "status": "running",
            "symbol": SYMBOL,
            "uk_time": now.isoformat(),
            "strategy": (
                "Balanced score model: mandatory H1 bias plus scored M15, "
                "M5 structure, RSI, ADX/DI/ATR and candle confirmation"
            ),
            "scout_timezone": SCOUT_TIMEZONE,
            "scout_windows": SCOUT_WINDOWS,
            "currently_scouting": is_within_scouting_time(),
            "weekend_sleep": "Friday 22:00 to Sunday 22:00",
            "sleeping_now": is_weekend_sleep(),
            "cooldown_minutes": SIGNAL_COOLDOWN_MINUTES,
            "minimum_score": MIN_SIGNAL_SCORE,
            "minimum_adx": MIN_ADX,
            "send_watchlist_signals": SEND_WATCHLIST_SIGNALS,
            "telegram_policy": (
                "A+/A- execution alerts and optional B+ watchlist alerts"
            ),
            "lot_sizes": {
                "A+": "0.50 lot",
                "A-": "0.25 lot",
                "B+": "0.15 lot — watchlist only",
                "Below B+": "No alert",
            },
            "last_signal_side": state["last_signal_side"],
            "last_candle_time": state["last_candle_time"],
        }
    )


@app.route("/run-once")
def manual_run():
    return jsonify(run_once())


@app.route("/last")
def last_result():
    return jsonify(
        state["last_result"]
        or {"status": "NO_RESULT", "reason": "No analysis has completed yet"}
    )


@app.route("/test-telegram")
def test_telegram():
    result = send_telegram("✅ XAUUSD bot test message received.")
    return jsonify(result)


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


# One Gunicorn worker is required so that only one background loop runs.
threading.Thread(target=bot_loop, daemon=True).start()
