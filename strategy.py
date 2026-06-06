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


def build_signal(m5_raw: pd.DataFrame, m15_raw: pd.DataFrame) -> Signal:
    m5 = add_indicators(m5_raw)
    m15 = add_indicators(m15_raw)
    c5, p5 = _latest(m5)
    c15, p15 = _latest(m15)

    price = float(c5.close)
    atr_like = max(6.0, abs(float(c5.bb_upper) - float(c5.bb_lower)) / 2)

    # Trend + momentum checks based on your manual confirmation rules
    m15_bull = c15.close > c15.bb_mid and c15.rsi > 50
    m15_bear = c15.close < c15.bb_mid and c15.rsi < 50
    m5_bull = c5.close > c5.bb_mid and c5.rsi > 50 and c5.stoch_k > c5.stoch_d
    m5_bear = c5.close < c5.bb_mid and c5.rsi < 50 and c5.stoch_k < c5.stoch_d

    near_upper = c5.close >= c5.bb_upper * 0.998
    near_lower = c5.close <= c5.bb_lower * 1.002

    # BUY: M15 not bearish + M5 reclaim momentum, preferably after pullback/hold.
    if m15_bull and m5_bull and not near_upper:
        entry = price
        sl = min(float(c5.bb_mid), price - atr_like * 0.75)
        risk = entry - sl
        tp1 = entry + max(6.0, risk * 1.0)
        tp2 = entry + max(10.0, risk * 1.7)
        return Signal(
            direction="BUY",
            confidence="A-" if c15.rsi > 55 else "B+",
            lot_size=settings.NORMAL_LOT if c15.rsi < 62 else min(settings.AGGRESSIVE_LOT, settings.MAX_LOT),
            entry=f"{_fmt(entry)} after candle close/hold above BB midline",
            take_profit=f"TP1 {_fmt(tp1)} | TP2 {_fmt(tp2)}",
            stop_loss=_fmt(sl),
            key_points=[
                f"M15 RSI {_fmt(c15.rsi)} above 50",
                "M5 and M15 aligned bullish",
                "Price above Bollinger midline",
                "Stochastic crossing upward",
            ],
            why="Momentum has shifted bullish across M5 and M15, with price holding above the Bollinger midline rather than rejecting.",
            invalidation=f"M5 closes back below {_fmt(sl)} or RSI loses 50.",
            signature=f"BUY-{c5.datetime}-{round(price)}",
        )

    # SELL: M15 weak + M5 rejecting/continuing down. Avoid chasing far outside lower band.
    if m15_bear and m5_bear and not near_lower:
        entry = price
        sl = max(float(c5.bb_mid), price + atr_like * 0.75)
        risk = sl - entry
        tp1 = entry - max(6.0, risk * 1.0)
        tp2 = entry - max(10.0, risk * 1.7)
        return Signal(
            direction="SELL",
            confidence="A-" if c15.rsi < 45 else "B+",
            lot_size=settings.NORMAL_LOT if c15.rsi > 38 else min(settings.AGGRESSIVE_LOT, settings.MAX_LOT),
            entry=f"{_fmt(entry)} after bearish candle close/retest failure",
            take_profit=f"TP1 {_fmt(tp1)} | TP2 {_fmt(tp2)}",
            stop_loss=_fmt(sl),
            key_points=[
                f"M15 RSI {_fmt(c15.rsi)} below 50",
                "M5 and M15 aligned bearish",
                "Price below Bollinger midline",
                "Stochastic crossing downward",
            ],
            why="Bearish structure remains in control, with price below the Bollinger midline and momentum confirming continuation.",
            invalidation=f"M5 closes back above {_fmt(sl)} or RSI reclaims 50.",
            signature=f"SELL-{c5.datetime}-{round(price)}",
        )

    # Oversold bounce caution: wait rather than automatic buy.
    key = [
        f"M5 RSI {_fmt(c5.rsi)}, M15 RSI {_fmt(c15.rsi)}",
        f"M5 stoch K/D {_fmt(c5.stoch_k)}/{_fmt(c5.stoch_d)}",
        "M5 and M15 not fully aligned",
        "Market is in transition/chop or extended near a band",
    ]

    if m15_bear and near_lower:
        why = "Bearish pressure is strong, but price is extended near the lower Bollinger Band. Chasing sells here risks selling the low. Wait for retracement rejection."
        invalid = f"A clean M5/M15 reclaim above {_fmt(c5.bb_mid)} with RSI above 50 can shift bias bullish."
    elif m15_bull and near_upper:
        why = "Bullish pressure exists, but price is extended near the upper Bollinger Band. Chasing buys here risks buying the top. Wait for pullback hold."
        invalid = f"A clean M5 close below {_fmt(c5.bb_mid)} with RSI below 50 can shift bias bearish."
    else:
        why = "Current candles do not give enough confirmation. Wait for M5 and M15 alignment before entering."
        invalid = "Bias becomes tradable only when candle closes confirm above/below structure with RSI and stochastic agreement."

    return Signal(
        direction="WAIT",
        confidence="No Trade",
        lot_size=0.0,
        entry="No entry yet",
        take_profit="N/A",
        stop_loss="N/A",
        key_points=key,
        why=why,
        invalidation=invalid,
        signature=f"WAIT-{c5.datetime}-{round(price)}",
    )


def format_signal(sig: Signal) -> str:
    kp = "\n".join(f"- {x}" for x in sig.key_points)
    lot = "No trade" if sig.direction == "WAIT" else f"{sig.lot_size:.2f} lot"
    return f"""<b>XAUUSD Signal</b>

<b>Best Scenario</b>
{sig.direction} ({sig.confidence})

<b>Best Lot Size</b>
{lot}

<b>Entry</b>
{sig.entry}

<b>Take Profit</b>
{sig.take_profit}

<b>Stop Loss</b>
{sig.stop_loss}

<b>Key Points</b>
{kp}

<b>Why this bias</b>
{sig.why}

<b>What can invalidate this bias</b>
{sig.invalidation}
"""
