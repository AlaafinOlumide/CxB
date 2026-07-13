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

POLL_INTERVAL_SECONDS = int(
    os.getenv("POLL_INTERVAL_SECONDS", "300")
)

SIGNAL_COOLDOWN_MINUTES = int(
    os.getenv("SIGNAL_COOLDOWN_MINUTES", "30")
)

MIN_SIGNAL_SCORE = int(
    os.getenv("MIN_SIGNAL_SCORE", "5")
)

MIN_ADX = float(
    os.getenv("MIN_ADX", "20")
)

SCOUT_TIMEZONE = os.getenv(
    "SCOUT_TIMEZONE",
    "Europe/London",
)

# Approved UK trading windows
SCOUT_WINDOWS = [
    ("23:00", "03:00"),
    ("07:00", "10:00"),
    ("13:30", "16:00"),
]

state = {
    "last_signal_time": None,
    "last_candle_time": None,
    "last_signal_side": None,
}


# ============================================================
# TIME AND SESSION CONTROL
# ============================================================

def london_now():
    return datetime.now(
        ZoneInfo(SCOUT_TIMEZONE)
    )


def is_weekend_sleep():
    """
    Weekend shutdown:
    Friday 22:00 UK time until Sunday 22:00 UK time.
    """

    now = london_now()
    weekday = now.weekday()
    current_time = now.time()

    boundary = datetime.strptime(
        "22:00",
        "%H:%M",
    ).time()

    # Friday from 22:00 onward
    if weekday == 4 and current_time >= boundary:
        return True

    # All Saturday
    if weekday == 5:
        return True

    # Sunday before 22:00
    if weekday == 6 and current_time < boundary:
        return True

    return False


def is_within_scouting_time():
    """
    Supports normal and overnight windows.
    """

    current_time = london_now().time()

    for start_text, end_text in SCOUT_WINDOWS:
        start = datetime.strptime(
            start_text,
            "%H:%M",
        ).time()

        end = datetime.strptime(
            end_text,
            "%H:%M",
        ).time()

        # Normal window, e.g. 07:00–10:00
        if start < end:
            if start <= current_time <= end:
                return True

        # Overnight window, e.g. 23:00–03:00
        else:
            if current_time >= start or current_time <= end:
                return True

    return False


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_CHAT_ID"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    response = requests.post(
        url,
        json=payload,
        timeout=20,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram error: {result}"
        )

    return result


# ============================================================
# TWELVE DATA
# ============================================================

def get_data(interval):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "Missing TWELVE_DATA_API_KEY"
        )

    url = (
        "https://api.twelvedata.com/"
        "time_series"
    )

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": 120,
        "apikey": TWELVE_DATA_API_KEY,
    }

    response = requests.get(
        url,
        params=params,
        timeout=25,
    )

    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(
            f"Twelve Data error for {interval}: {data}"
        )

    df = pd.DataFrame(
        data["values"]
    )

    required_columns = {
        "datetime",
        "open",
        "high",
        "low",
        "close",
    }

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise RuntimeError(
            f"Missing columns: {missing_columns}"
        )

    df = df.rename(
        columns={"datetime": "time"}
    )

    df["time"] = pd.to_datetime(
        df["time"]
    )

    df = df.sort_values("time")

    for column in [
        "open",
        "high",
        "low",
        "close",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
        ]
    )

    return df.reset_index(drop=True)


# ============================================================
# INDICATORS
# ============================================================

def calculate_indicators(df):
    df = df.copy()

    # Bollinger Bands
    df["bb_mid"] = (
        df["close"]
        .rolling(20)
        .mean()
    )

    df["bb_std"] = (
        df["close"]
        .rolling(20)
        .std()
    )

    df["bb_upper"] = (
        df["bb_mid"]
        + (2 * df["bb_std"])
    )

    df["bb_lower"] = (
        df["bb_mid"]
        - (2 * df["bb_std"])
    )

    df["bb_width"] = (
        df["bb_upper"]
        - df["bb_lower"]
    )

    # RSI using Wilder-style smoothing
    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    average_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    average_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    relative_strength = (
        average_gain
        / average_loss.replace(
            0,
            float("nan"),
        )
    )

    df["rsi"] = (
        100
        - (
            100
            / (1 + relative_strength)
        )
    )

    # Stochastic
    lowest_low = (
        df["low"]
        .rolling(14)
        .min()
    )

    highest_high = (
        df["high"]
        .rolling(14)
        .max()
    )

    stochastic_range = (
        highest_high
        - lowest_low
    ).replace(
        0,
        float("nan"),
    )

    df["stoch_k"] = (
        100
        * (
            (
                df["close"]
                - lowest_low
            )
            / stochastic_range
        )
    )

    df["stoch_d"] = (
        df["stoch_k"]
        .rolling(3)
        .mean()
    )

    # ATR
    previous_close = (
        df["close"]
        .shift(1)
    )

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (
                df["high"]
                - previous_close
            ).abs(),
            (
                df["low"]
                - previous_close
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["true_range"] = true_range

    df["atr"] = true_range.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    # ADX and directional indicators
    up_move = (
        df["high"]
        - df["high"].shift(1)
    )

    down_move = (
        df["low"].shift(1)
        - df["low"]
    )

    plus_dm = up_move.where(
        (up_move > down_move)
        & (up_move > 0),
        0.0,
    )

    minus_dm = down_move.where(
        (down_move > up_move)
        & (down_move > 0),
        0.0,
    )

    smoothed_plus_dm = plus_dm.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    smoothed_minus_dm = minus_dm.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    atr_safe = df["atr"].replace(
        0,
        float("nan"),
    )

    df["plus_di"] = (
        100
        * smoothed_plus_dm
        / atr_safe
    )

    df["minus_di"] = (
        100
        * smoothed_minus_dm
        / atr_safe
    )

    di_sum = (
        df["plus_di"]
        + df["minus_di"]
    ).replace(
        0,
        float("nan"),
    )

    df["dx"] = (
        100
        * (
            (
                df["plus_di"]
                - df["minus_di"]
            ).abs()
            / di_sum
        )
    )

    df["adx"] = df["dx"].ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    df = df.replace(
        [float("inf"), -float("inf")],
        float("nan"),
    )

    return df.dropna().reset_index(
        drop=True
    )


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
        return "WATCH CLOSELY"

    return "WAIT"


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
        and candle["rsi"] >= 53
        and candle["plus_di"] > candle["minus_di"]
    )

    bearish = (
        candle["close"] < candle["bb_mid"]
        and candle["rsi"] <= 47
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

    if adx >= 20:
        return "Moderate"

    return "Weak"


def atr_is_healthy(current, previous):
    if previous["atr"] <= 0:
        return False

    atr_ratio = (
        current["atr"]
        / previous["atr"]
    )

    # Relaxed from 0.95 to 0.90
    return atr_ratio >= 0.90


def momentum_is_valid(
    side,
    current,
    previous,
):
    # ADX may be stable or only slightly lower
    adx_stable_or_rising = (
        current["adx"]
        >= previous["adx"] * 0.98
    )

    adx_strong_enough = (
        current["adx"] >= MIN_ADX
    )

    if side == "BUY":
        direction_valid = (
            current["plus_di"]
            > current["minus_di"]
        )
    else:
        direction_valid = (
            current["minus_di"]
            > current["plus_di"]
        )

    return (
        adx_strong_enough
        and adx_stable_or_rising
        and direction_valid
    )


# ============================================================
# CANDLE PATTERNS
# ============================================================

def candle_body(candle):
    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(candle):
    return max(
        candle["high"]
        - candle["low"],
        0.0001,
    )


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
    body = max(
        candle_body(candle),
        0.0001,
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    return (
        candle["close"] > candle["open"]
        and lower_wick >= body * 1.3
        and lower_wick > upper_wick
    )


def bearish_rejection(candle):
    body = max(
        candle_body(candle),
        0.0001,
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )

    return (
        candle["close"] < candle["open"]
        and upper_wick >= body * 1.3
        and upper_wick > lower_wick
    )


def bullish_breakout_retest(
    current,
    previous,
    older,
):
    breakout_level = max(
        older["high"],
        older["bb_mid"],
    )

    return (
        previous["close"] > breakout_level
        and current["low"] <= breakout_level
        and current["close"] > breakout_level
        and current["close"] > current["open"]
    )


def bearish_breakout_retest(
    current,
    previous,
    older,
):
    breakout_level = min(
        older["low"],
        older["bb_mid"],
    )

    return (
        previous["close"] < breakout_level
        and current["high"] >= breakout_level
        and current["close"] < breakout_level
        and current["close"] < current["open"]
    )


def strong_directional_candle(
    side,
    candle,
):
    body = candle_body(candle)
    total_range = candle_range(candle)

    body_ratio = (
        body
        / total_range
    )

    if side == "BUY":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.60
            and candle["close"] > candle["bb_mid"]
        )

    if side == "SELL":
        return (
            candle["close"] < candle["open"]
            and body_ratio >= 0.60
            and candle["close"] < candle["bb_mid"]
        )

    return False


def confirmation_type(
    side,
    current,
    previous,
    older,
):
    if side == "BUY":
        if bullish_engulfing(
            current,
            previous,
        ):
            return "Bullish engulfing"

        if bullish_rejection(current):
            return "Bullish rejection"

        if bullish_breakout_retest(
            current,
            previous,
            older,
        ):
            return "Breakout and retest"

        if strong_directional_candle(
            "BUY",
            current,
        ):
            return "Strong bullish candle close"

    if side == "SELL":
        if bearish_engulfing(
            current,
            previous,
        ):
            return "Bearish engulfing"

        if bearish_rejection(current):
            return "Bearish rejection"

        if bearish_breakout_retest(
            current,
            previous,
            older,
        ):
            return "Breakdown and retest"

        if strong_directional_candle(
            "SELL",
            current,
        ):
            return "Strong bearish candle close"

    return None


# ============================================================
# EXHAUSTION AND CHASING FILTERS
# ============================================================

def exhaustion_risk(
    side,
    current,
    previous,
):
    atr = float(current["atr"])

    if atr <= 0:
        return "High"

    current_range = candle_range(
        current
    )

    large_expansion = (
        current_range >= atr * 1.5
    )

    atr_falling = (
        current["atr"]
        < previous["atr"] * 0.90
    )

    if side == "BUY":
        if (
            current["rsi"] >= 72
            and current["close"] >= current["bb_upper"]
            and large_expansion
        ):
            return "High"

        if (
            current["rsi"] >= 68
            and (
                current["close"] >= current["bb_upper"]
                or atr_falling
            )
        ):
            return "Medium"

    if side == "SELL":
        if (
            current["rsi"] <= 28
            and current["close"] <= current["bb_lower"]
            and large_expansion
        ):
            return "High"

        if (
            current["rsi"] <= 32
            and (
                current["close"] <= current["bb_lower"]
                or atr_falling
            )
        ):
            return "Medium"

    return "Low"


def not_chasing_extreme(
    side,
    candle,
):
    atr = float(candle["atr"])

    if atr <= 0:
        return False

    if side == "BUY":
        distance_from_midline = (
            candle["close"]
            - candle["bb_mid"]
        )

        return (
            distance_from_midline
            <= atr * 1.4
        )

    if side == "SELL":
        distance_from_midline = (
            candle["bb_mid"]
            - candle["close"]
        )

        return (
            distance_from_midline
            <= atr * 1.4
        )

    return False


# ============================================================
# ANALYSIS
# ============================================================

def analyse():
    h1 = calculate_indicators(
        get_data("1h")
    )

    m15 = calculate_indicators(
        get_data("15min")
    )

    m5 = calculate_indicators(
        get_data("5min")
    )

    if (
        len(h1) < 4
        or len(m15) < 4
        or len(m5) < 5
    ):
        raise RuntimeError(
            "Insufficient indicator data"
        )

    # Use closed candles only
    h1_current = h1.iloc[-2]
    m15_current = m15.iloc[-2]

    m5_current = m5.iloc[-2]
    m5_previous = m5.iloc[-3]
    m5_older = m5.iloc[-4]

    higher_timeframe_bias = h1_bias(
        h1_current
    )

    if higher_timeframe_bias == "NEUTRAL":
        return {
            "side": "WAIT",
            "reason": "H1 trend is neutral",
            "h1_rsi": round(
                float(h1_current["rsi"]),
                2,
            ),
            "candle_time": str(
                m5_current["time"]
            ),
        }

    side = higher_timeframe_bias

    conditions = []
    key_points = []

    # 1. H1 direction
    conditions.append(True)

    if side == "BUY":
        key_points.append(
            "H1 bullish: buyers control higher timeframe"
        )
    else:
        key_points.append(
            "H1 bearish: sellers control higher timeframe"
        )

    # 2. M15 alignment
    if side == "BUY":
        m15_condition = (
            m15_current["close"]
            > m15_current["bb_mid"]
            and m15_current["rsi"] >= 50
            and m15_current["plus_di"]
            > m15_current["minus_di"]
        )
    else:
        m15_condition = (
            m15_current["close"]
            < m15_current["bb_mid"]
            and m15_current["rsi"] <= 50
            and m15_current["minus_di"]
            > m15_current["plus_di"]
        )

    conditions.append(
        m15_condition
    )

    if not m15_condition:
        return {
            "side": "WAIT",
            "reason": "M15 is not aligned with H1",
            "h1_bias": higher_timeframe_bias,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    if side == "BUY":
        key_points.append(
            "M15 bullish and aligned with H1"
        )
    else:
        key_points.append(
            "M15 bearish and aligned with H1"
        )

    # 3. M5 structure
    if side == "BUY":
        m5_structure = (
            m5_current["close"]
            > m5_current["bb_mid"]
        )
    else:
        m5_structure = (
            m5_current["close"]
            < m5_current["bb_mid"]
        )

    conditions.append(
        m5_structure
    )

    if not m5_structure:
        return {
            "side": "WAIT",
            "reason": "M5 structure has not confirmed",
            "h1_bias": higher_timeframe_bias,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    if side == "BUY":
        key_points.append(
            "M5 closed above Bollinger midline"
        )
    else:
        key_points.append(
            "M5 closed below Bollinger midline"
        )

    # 4. RSI confirmation
    rsi_rising = (
        m5_current["rsi"]
        > m5_previous["rsi"]
    )

    rsi_falling = (
        m5_current["rsi"]
        < m5_previous["rsi"]
    )

    if side == "BUY":
        rsi_condition = (
            m5_current["rsi"] >= 50
            and rsi_rising
        )
    else:
        rsi_condition = (
            m5_current["rsi"] <= 50
            and rsi_falling
        )

    conditions.append(
        rsi_condition
    )

    if not rsi_condition:
        return {
            "side": "WAIT",
            "reason": "M5 RSI has not confirmed momentum",
            "h1_bias": higher_timeframe_bias,
            "m5_rsi": round(
                float(m5_current["rsi"]),
                2,
            ),
            "candle_time": str(
                m5_current["time"]
            ),
        }

    if side == "BUY":
        key_points.append(
            f"M5 RSI {m5_current['rsi']:.2f} "
            "above 50 and rising"
        )
    else:
        key_points.append(
            f"M5 RSI {m5_current['rsi']:.2f} "
            "below 50 and falling"
        )

    # 5. ADX, DI and ATR
    momentum_condition = (
        momentum_is_valid(
            side,
            m5_current,
            m5_previous,
        )
        and atr_is_healthy(
            m5_current,
            m5_previous,
        )
    )

    conditions.append(
        momentum_condition
    )

    if not momentum_condition:
        return {
            "side": "WAIT",
            "reason": (
                "ADX, DI direction or ATR "
                "has not confirmed momentum"
            ),
            "h1_bias": higher_timeframe_bias,
            "adx": round(
                float(m5_current["adx"]),
                2,
            ),
            "previous_adx": round(
                float(m5_previous["adx"]),
                2,
            ),
            "candle_time": str(
                m5_current["time"]
            ),
        }

    strength = momentum_strength(
        m5_current
    )

    key_points.append(
        f"ADX {m5_current['adx']:.2f}: "
        f"momentum is {strength}"
    )

    if side == "BUY":
        key_points.append(
            "+DI is above -DI"
        )
    else:
        key_points.append(
            "-DI is above +DI"
        )

    # 6. Candle confirmation
    confirmation = confirmation_type(
        side,
        m5_current,
        m5_previous,
        m5_older,
    )

    chase_condition = not_chasing_extreme(
        side,
        m5_current,
    )

    confirmation_condition = (
        confirmation is not None
        and chase_condition
    )

    conditions.append(
        confirmation_condition
    )

    if not confirmation_condition:
        return {
            "side": "WAIT",
            "reason": (
                "No valid confirmation candle "
                "or price has moved too far"
            ),
            "h1_bias": higher_timeframe_bias,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    key_points.append(
        f"{confirmation} confirmed"
    )

    if side == "BUY":
        key_points.append(
            "Not chasing an extended bullish move"
        )
    else:
        key_points.append(
            "Not chasing an extended bearish move"
        )

    exhaustion = exhaustion_risk(
        side,
        m5_current,
        m5_previous,
    )

    if exhaustion == "High":
        return {
            "side": "WAIT",
            "reason": "Momentum exhaustion risk is high",
            "h1_bias": higher_timeframe_bias,
            "exhaustion_risk": exhaustion,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    score = sum(
        1
        for condition in conditions
        if condition
    )

    if score < MIN_SIGNAL_SCORE:
        return {
            "side": "WAIT",
            "reason": (
                f"Score {score}/6 is below "
                f"minimum {MIN_SIGNAL_SCORE}/6"
            ),
            "h1_bias": higher_timeframe_bias,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    grade = grade_from_score(
        score
    )

    quality = quality_from_score(
        score
    )

    if quality != "EXECUTION GRADE":
        return {
            "side": "WAIT",
            "reason": "Signal is not Execution Grade",
            "score": score,
            "candle_time": str(
                m5_current["time"]
            ),
        }

    price = round(
        float(m5_current["close"]),
        2,
    )

    atr = float(
        m5_current["atr"]
    )

    entry_buffer = max(
        round(atr * 0.20, 2),
        0.50,
    )

    stop_buffer = max(
        round(atr * 0.25, 2),
        0.75,
    )

    if side == "BUY":
        emoji = "🟢"

        entry_low = price

        entry_high = round(
            price + entry_buffer,
            2,
        )

        structural_stop = min(
            float(m5_current["low"]),
            float(m5_current["bb_mid"]),
        )

        stop_loss = round(
            structural_stop - stop_buffer,
            2,
        )

        risk = entry_low - stop_loss

        if risk <= 0:
            return {
                "side": "WAIT",
                "reason": "Invalid BUY risk calculation",
                "candle_time": str(
                    m5_current["time"]
                ),
            }

        take_profit_1 = round(
            entry_low + risk,
            2,
        )

        take_profit_2 = round(
            entry_low + (risk * 1.8),
            2,
        )

        entry_text = (
            f"{entry_low:.2f} - "
            f"{entry_high:.2f} "
            f"after {confirmation.lower()}"
        )

        why = (
            "H1 is bullish, M15 agrees, and M5 "
            "has produced a confirmed bullish trigger. "
            "ADX and +DI support the move while ATR "
            "shows acceptable volatility."
        )

        invalidation = (
            f"Bias fails if M5 closes below "
            f"{stop_loss:.2f}, RSI loses 50, "
            f"ADX falls below {MIN_ADX:.0f}, "
            "or H1 loses bullish structure."
        )

    else:
        emoji = "🔴"

        entry_high = price

        entry_low = round(
            price - entry_buffer,
            2,
        )

        structural_stop = max(
            float(m5_current["high"]),
            float(m5_current["bb_mid"]),
        )

        stop_loss = round(
            structural_stop + stop_buffer,
            2,
        )

        risk = stop_loss - entry_high

        if risk <= 0:
            return {
                "side": "WAIT",
                "reason": "Invalid SELL risk calculation",
                "candle_time": str(
                    m5_current["time"]
                ),
            }

        take_profit_1 = round(
            entry_high - risk,
            2,
        )

        take_profit_2 = round(
            entry_high - (risk * 1.8),
            2,
        )

        entry_text = (
            f"{entry_low:.2f} - "
            f"{entry_high:.2f} "
            f"after {confirmation.lower()}"
        )

        why = (
            "H1 is bearish, M15 agrees, and M5 "
            "has produced a confirmed bearish trigger. "
            "ADX and -DI support the move while ATR "
            "shows acceptable volatility."
        )

        invalidation = (
            f"Bias fails if M5 closes above "
            f"{stop_loss:.2f}, RSI reclaims 50, "
            f"ADX falls below {MIN_ADX:.0f}, "
            "or H1 turns bullish."
        )

    lot = lot_size_from_score(
        score
    )

    lot_text = f"{lot:.2f} lot"

    message = (
        f"{emoji} XAUUSD SIGNAL\n\n"
        f"Best Scenario\n"
        f"{side} ({grade} | Score {score}/6)\n\n"
        f"Quality\n"
        f"{quality}\n\n"
        f"Momentum Strength\n"
        f"{strength}\n\n"
        f"Exhaustion Risk\n"
        f"{exhaustion}\n\n"
        f"Entry\n"
        f"{entry_text}\n\n"
        f"Take Profit\n"
        f"TP1 {take_profit_1:.2f} | "
        f"TP2 {take_profit_2:.2f}\n\n"
        f"Stop Loss\n"
        f"{stop_loss:.2f}\n\n"
        f"Best Lot Size\n"
        f"{lot_text}\n\n"
        f"Key Points\n"
        + "\n".join(
            f"• {point}"
            for point in key_points
        )
        + "\n\n"
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
        "h1_bias": higher_timeframe_bias,
        "momentum_strength": strength,
        "exhaustion_risk": exhaustion,
        "confirmation": confirmation,
        "entry": entry_text,
        "stop_loss": stop_loss,
        "tp1": take_profit_1,
        "tp2": take_profit_2,
        "lot_size": lot,
        "message": message,
        "candle_time": str(
            m5_current["time"]
        ),
    }


# ============================================================
# SIGNAL DELIVERY CONTROL
# ============================================================

def should_send(signal):
    if not signal:
        return False

    # Do not send WAIT messages
    if signal.get("side") == "WAIT":
        return False

    # Execution Grade only
    if signal.get("quality") != "EXECUTION GRADE":
        return False

    candle_time = signal.get(
        "candle_time"
    )

    # Prevent duplicate signal from same closed candle
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
    if is_weekend_sleep():
        return {
            "status": "SLEEPING",
            "reason": "Weekend shutdown active",
            "sleep_window": (
                "Friday 22:00 to Sunday 22:00"
            ),
            "timezone": SCOUT_TIMEZONE,
        }

    if not is_within_scouting_time():
        return {
            "status": "OFF_SESSION",
            "reason": (
                "Outside approved scouting windows"
            ),
            "scouting_windows": SCOUT_WINDOWS,
            "timezone": SCOUT_TIMEZONE,
        }

    signal = analyse()

    if should_send(signal):
        send_telegram(
            signal["message"]
        )

        state["last_signal_time"] = (
            datetime.now(timezone.utc)
        )

        state["last_candle_time"] = (
            signal["candle_time"]
        )

        state["last_signal_side"] = (
            signal["side"]
        )

        print(
            (
                f"Signal sent: "
                f"{signal['side']} "
                f"{signal['grade']} "
                f"{signal['score']}/6"
            ),
            flush=True,
        )

        return signal

    return {
        "status": "WAIT",
        "reason": signal.get(
            "reason",
            (
                "No Execution Grade signal, "
                "duplicate candle, or cooldown active"
            ),
        ),
        "analysis": signal,
    }


# ============================================================
# BACKGROUND LOOP
# ============================================================

def bot_loop():
    time.sleep(10)

    while True:
        try:
            result = run_once()

            print(
                result,
                flush=True,
            )

        except Exception as error:
            print(
                f"Bot error: {error}",
                flush=True,
            )

        time.sleep(
            POLL_INTERVAL_SECONDS
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/")
def home():
    now = london_now()

    return jsonify({
        "status": "running",
        "symbol": SYMBOL,
        "uk_time": now.isoformat(),
        "strategy": (
            "H1 trend + M15 alignment + "
            "M5 confirmation + ADX/DI + "
            "ATR + exhaustion filter"
        ),
        "scout_timezone": SCOUT_TIMEZONE,
        "scout_windows": SCOUT_WINDOWS,
        "currently_scouting": (
            is_within_scouting_time()
        ),
        "weekend_sleep": (
            "Friday 22:00 to Sunday 22:00"
        ),
        "sleeping_now": (
            is_weekend_sleep()
        ),
        "cooldown_minutes": (
            SIGNAL_COOLDOWN_MINUTES
        ),
        "minimum_score": MIN_SIGNAL_SCORE,
        "minimum_adx": MIN_ADX,
        "telegram_policy": (
            "Execution Grade only"
        ),
        "lot_sizes": {
            "A+": "0.50 lot",
            "A-": "0.25 lot",
            "B+": "0.15 lot - not sent",
            "B/WAIT": "No Trade",
        },
    })


@app.route("/run-once")
def manual_run():
    return jsonify(
        run_once()
    )


@app.route("/test-telegram")
def test_telegram():
    result = send_telegram(
        "✅ XAUUSD bot test message received."
    )

    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    })


# Start one background loop
threading.Thread(
    target=bot_loop,
    daemon=True,
).start()