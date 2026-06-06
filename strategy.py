from dataclasses import dataclass
from typing import Literal
import pandas as pd
from config import settings
from indicators import add_indicators

Direction = Literal["BUY", "SELL", "WAIT"]

@dataclass
class Signal:
    direction: Direction
    confidence: str
    score: int
    lot_size: float
    entry: str
    take_profit: str
    stop_loss: str
    key_points: list[str]
    why: str
    invalidation: str
    signature: str


def _round_price(x: float) -> float:
    return round(float(x), 2)


def _fmt(x: float) -> str:
    return f"{_round_price(x):.2f}"


def _latest(df: pd.DataFrame):
    return df.iloc[-1], df.iloc[-2]


def _cross_up(c, p, k="stoch_k", d="stoch_d") -> bool:
    return float(p[k]) <= float(p[d]) and float(c[k]) > float(c[d])


def _cross_down(c, p, k="stoch_k", d="stoch_d") -> bool:
    return float(p[k]) >= float(p[d]) and float(c[k]) < float(c[d])


def _bullish_candle(c) -> bool:
    body = float(c.close) - float(c.open)
    rng = max(float(c.high) - float(c.low), 0.01)
    return body > 0 and body / rng >= 0.35


def _bearish_candle(c) -> bool:
    body = float(c.open) - float(c.close)
    rng = max(float(c.high) - float(c.low), 0.01)
    return body > 0 and body / rng >= 0.35


def _lot_for_risk(entry: float, sl: float, confidence: str) -> float:
    stop_points = max(abs(entry - sl), 0.01)
    raw = settings.MAX_RISK_PER_TRADE / (stop_points * settings.DOLLARS_PER_1_00_LOT_PER_POINT)
    cap = settings.AGGRESSIVE_LOT if confidence.startswith("A") else settings.NORMAL_LOT
    lot = min(raw, cap, settings.MAX_LOT)
    return max(0.01, round(lot, 2))


def _confidence(score: int) -> str:
    if score >= 6:
        return "A"
    if score == 5:
        return "A-"
    if score == 4:
        return "B+"
    return "No Trade"


def build_signal(m5_raw: pd.DataFrame, m15_raw: pd.DataFrame) -> Signal:
    m5 = add_indicators(m5_raw)
    m15 = add_indicators(m15_raw)
    c5, p5 = _latest(m5)
    c15, p15 = _latest(m15)

    price = float(c5.close)
    bb_width = max(float(c5.bb_upper) - float(c5.bb_lower), 1.0)
    buffer = max(2.0, bb_width * 0.18)

    m15_bull = c15.close > c15.bb_mid and c15.rsi > 50
    m15_bear = c15.close < c15.bb_mid and c15.rsi < 50
    m15_recovering = c15.rsi > p15.rsi and c15.close >= p15.close
    m15_weakening = c15.rsi < p15.rsi and c15.close <= p15.close

    stoch_up = c5.stoch_k > c5.stoch_d and c5.stoch_k > p5.stoch_k
    stoch_down = c5.stoch_k < c5.stoch_d and c5.stoch_k < p5.stoch_k
    fresh_stoch_buy = _cross_up(c5, p5) or (stoch_up and 20 <= c5.stoch_k <= 75)
    fresh_stoch_sell = _cross_down(c5, p5) or (stoch_down and 25 <= c5.stoch_k <= 80)

    above_mid = c5.close > c5.bb_mid
    below_mid = c5.close < c5.bb_mid
    near_upper = c5.close >= c5.bb_upper - 1.0
    near_lower = c5.close <= c5.bb_lower + 1.0

    buy_score = 0
    buy_points = []
    if m15_bull or (not m15_bear and m15_recovering):
        buy_score += 1; buy_points.append("M15 bullish/recovering")
    if above_mid:
        buy_score += 1; buy_points.append("M5 closed above Bollinger midline")
    if c5.rsi > 50 and c5.rsi > p5.rsi:
        buy_score += 1; buy_points.append(f"M5 RSI {_fmt(c5.rsi)} above 50 and rising")
    if fresh_stoch_buy:
        buy_score += 1; buy_points.append("Stochastic bullish cross/momentum")
    if _bullish_candle(c5):
        buy_score += 1; buy_points.append("Bullish candle close confirmed")
    if not near_upper:
        buy_score += 1; buy_points.append("Not chasing upper Bollinger Band")

    sell_score = 0
    sell_points = []
    if m15_bear or (not m15_bull and m15_weakening):
        sell_score += 1; sell_points.append("M15 bearish/weakening")
    if below_mid:
        sell_score += 1; sell_points.append("M5 closed below Bollinger midline")
    if c5.rsi < 50 and c5.rsi < p5.rsi:
        sell_score += 1; sell_points.append(f"M5 RSI {_fmt(c5.rsi)} below 50 and falling")
    if fresh_stoch_sell:
        sell_score += 1; sell_points.append("Stochastic bearish cross/momentum")
    if _bearish_candle(c5):
        sell_score += 1; sell_points.append("Bearish candle close confirmed")
    if not near_lower:
        sell_score += 1; sell_points.append("Not chasing lower Bollinger Band")

    # Minimum alignment: 4/6; ideal A setup: 5-6/6. If conflict or weak score, WAIT.
    if buy_score >= 4 and buy_score > sell_score and not m15_bear:
        confidence = _confidence(buy_score)
        entry_low = price
        entry_high = price + 2.0
        sl = min(float(c5.bb_mid) - 1.5, price - buffer)
        risk = price - sl
        tp1 = price + max(5.0, risk * 1.0)
        tp2 = price + max(9.0, risk * 1.8)
        lot = _lot_for_risk(price, sl, confidence)
        return Signal(
            direction="BUY",
            confidence=confidence,
            score=buy_score,
            lot_size=lot,
            entry=f"{_fmt(entry_low)} - {_fmt(entry_high)} after candle close/hold",
            take_profit=f"TP1 {_fmt(tp1)} | TP2 {_fmt(tp2)}",
            stop_loss=_fmt(sl),
            key_points=buy_points,
            why="M5 trigger is bullish and M15 is not fighting the trade. Price has reclaimed structure instead of rejecting immediately.",
            invalidation=f"Bias fails if M5 closes below {_fmt(sl)} or RSI drops back under 50.",
            signature=f"BUY-{c5.datetime.strftime('%Y%m%d%H%M')}-{round(price)}",
        )

    if sell_score >= 4 and sell_score > buy_score and not m15_bull:
        confidence = _confidence(sell_score)
        entry_low = price - 2.0
        entry_high = price
        sl = max(float(c5.bb_mid) + 1.5, price + buffer)
        risk = sl - price
        tp1 = price - max(5.0, risk * 1.0)
        tp2 = price - max(9.0, risk * 1.8)
        lot = _lot_for_risk(price, sl, confidence)
        return Signal(
            direction="SELL",
            confidence=confidence,
            score=sell_score,
            lot_size=lot,
            entry=f"{_fmt(entry_low)} - {_fmt(entry_high)} after rejection/bearish close",
            take_profit=f"TP1 {_fmt(tp1)} | TP2 {_fmt(tp2)}",
            stop_loss=_fmt(sl),
            key_points=sell_points,
            why="M5 trigger is bearish and M15 is not fighting the trade. Price remains below structure with momentum confirming continuation.",
            invalidation=f"Bias fails if M5 closes above {_fmt(sl)} or RSI reclaims 50.",
            signature=f"SELL-{c5.datetime.strftime('%Y%m%d%H%M')}-{round(price)}",
        )

    key = [
        f"Buy score {buy_score}/6 | Sell score {sell_score}/6",
        f"M5 RSI {_fmt(c5.rsi)} | M15 RSI {_fmt(c15.rsi)}",
        f"M5 stoch K/D {_fmt(c5.stoch_k)}/{_fmt(c5.stoch_d)}",
        "Minimum alignment not met or M5/M15 conflict",
    ]
    return Signal(
        direction="WAIT",
        confidence="No Trade",
        score=max(buy_score, sell_score),
        lot_size=0.0,
        entry="No entry yet",
        take_profit="N/A",
        stop_loss="N/A",
        key_points=key,
        why="The setup does not meet minimum alignment. Waiting protects the prop account from chop, fakeouts, and emotional entries.",
        invalidation="Becomes tradable only when candle close, RSI, stochastic, Bollinger midline and M15 structure align.",
        signature=f"WAIT-{c5.datetime.strftime('%Y%m%d%H%M')}-{round(price)}",
    )


def format_signal(sig: Signal) -> str:
    kp = "\n".join(f"• {x}" for x in sig.key_points)
    lot = "No trade" if sig.direction == "WAIT" else f"{sig.lot_size:.2f} lot"
    emoji = "🟢" if sig.direction == "BUY" else "🔴" if sig.direction == "SELL" else "⚪"
    return f"""{emoji} <b>XAUUSD SIGNAL</b>

<b>Best Scenario</b>
{sig.direction} ({sig.confidence} | Score {sig.score}/6)

<b>Entry</b>
{sig.entry}

<b>Take Profit</b>
{sig.take_profit}

<b>Stop Loss</b>
{sig.stop_loss}

<b>Best Lot Size</b>
{lot}

<b>Key Points</b>
{kp}

<b>Why this bias</b>
{sig.why}

<b>What can invalidate this bias</b>
{sig.invalidation}
"""
