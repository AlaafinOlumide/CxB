"""
XAUUSD Signal Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Data      : TwelveData  (free: 8 req/min, 800/day)
Signals   : Telegram
Hosting   : Render (web service + keep-alive)
Strategy  : Trend-following confluence bot
            Bias from EMA21/50/200
            Entry confirmation from RSI + MACD + StochRSI + Bollinger Bands + ATR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import time
import math
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TWELVE_API_KEY   = os.getenv("TWELVE_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL      = "XAU/USD"
INTERVAL    = "15min"
CANDLES     = 250
MIN_BARS    = 250

TP1_RATIO   = 1.5
TP2_RATIO   = 3.0
ATR_MULT    = 1.2
THRESHOLD   = 4   # out of 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── FLASK KEEP-ALIVE ──────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "XAUUSD Signal Bot is running ✅"

@app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ─── TWELVEDATA ────────────────────────────────────────────────────────────────

def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": CANDLES,
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        if "values" not in data:
            log.error("TwelveData error: %s", data.get("message", data))
            return None

        candles = [
            {
                "time": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
            for c in data["values"]
        ]

        if len(candles) < MIN_BARS:
            log.warning("Fetched only %d candles, need at least %d", len(candles), MIN_BARS)

        return candles

    except Exception as e:
        log.error("fetch_candles error: %s", e)
        return None

# ─── INDICATORS ────────────────────────────────────────────────────────────────

def ema(values, period):
    k = 2 / (period + 1)
    result = [None] * len(values)
    if len(values) < period:
        return result

    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out

    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    ag = sum(gains) / period
    al = sum(losses) / period

    for i in range(period, len(closes)):
        if i > period:
            d = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(d, 0)) / period
            al = (al * (period - 1) + max(-d, 0)) / period

        rs = ag / al if al != 0 else 100
        out[i] = 100 - (100 / (1 + rs))

    return out


def macd(closes, fast=12, slow=26, sig=9):
    ef = ema(closes, fast)
    es = ema(closes, slow)

    line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ef, es)
    ]

    valid = [v for v in line if v is not None]
    if not valid:
        return [None] * len(closes), [None] * len(closes), [None] * len(closes)

    sig_r = ema(valid, sig)
    offset = next(i for i, v in enumerate(line) if v is not None)

    sig_f = [None] * len(line)
    for i, v in enumerate(sig_r):
        if offset + i < len(sig_f):
            sig_f[offset + i] = v

    hist = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(line, sig_f)
    ]

    return line, sig_f, hist


def atr(candles, period=14):
    trs = [None]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    out = [None] * len(trs)
    if len(trs) < period + 1:
        return out

    out[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period

    return out


def stoch_rsi(rsi_vals, period=14, smooth_k=3, smooth_d=3):
    stoch = [None] * len(rsi_vals)
    valid = [(i, v) for i, v in enumerate(rsi_vals) if v is not None]

    if len(valid) < period:
        return stoch, stoch

    for idx in range(period - 1, len(valid)):
        window = [valid[j][1] for j in range(idx - period + 1, idx + 1)]
        lo, hi = min(window), max(window)
        orig_i = valid[idx][0]
        stoch[orig_i] = ((window[-1] - lo) / (hi - lo) * 100) if hi != lo else 50

    def smooth(arr, p):
        out = [None] * len(arr)
        vals = [(i, v) for i, v in enumerate(arr) if v is not None]
        for idx in range(p - 1, len(vals)):
            avg = sum(vals[j][1] for j in range(idx - p + 1, idx + 1)) / p
            out[vals[idx][0]] = avg
        return out

    k_line = smooth(stoch, smooth_k)
    d_line = smooth(k_line, smooth_d)
    return k_line, d_line


def bollinger_bands(closes, period=20, std_mult=2.0):
    mid = [None] * len(closes)
    upper = [None] * len(closes)
    lower = [None] * len(closes)

    if len(closes) < period:
        return mid, upper, lower

    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(variance)

        mid[i] = mean
        upper[i] = mean + std_mult * std
        lower[i] = mean - std_mult * std

    return mid, upper, lower

# ─── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def analyse(candles):
    if len(candles) < MIN_BARS:
        log.warning("Not enough candles for EMA200 strategy: got %d, need %d", len(candles), MIN_BARS)
        return None

    # TwelveData usually returns newest first; reverse to oldest -> newest
    c = list(reversed(candles))
    closes = [x["close"] for x in c]
    i = len(closes) - 1

    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    rsi_ = rsi(closes, 14)
    ml, ms, mh = macd(closes)
    atr_ = atr(c, 14)
    sk, sd = stoch_rsi(rsi_)
    bb_mid, bb_upper, bb_lower = bollinger_bands(closes, 20, 2.0)

    price = closes[i]

    vals = [
        e21[i], e50[i], e200[i], rsi_[i], ml[i], ms[i], mh[i], atr_[i],
        bb_mid[i], bb_upper[i], bb_lower[i]
    ]
    if any(v is None for v in vals):
        log.warning("Indicators not ready on latest bar.")
        return None

    v21, v50, v200, vrsi, vml, vms, vmh, vatr, vbb_mid, vbb_upper, vbb_lower = vals
    kval = sk[i]
    dval = sd[i] if i < len(sd) else None

    # Trend / bias
    bull_trend_ok = price > v200 and v21 > v50
    bear_trend_ok = price < v200 and v21 < v50

    if bull_trend_ok:
        trend_status = "BULL"
    elif bear_trend_ok:
        trend_status = "BEAR"
    else:
        trend_status = "MIXED/RANGING"

    if trend_status == "MIXED/RANGING":
        log.info(
            "Score -> NONE | Trend:%s | RSI:%.1f | Price:%.2f | EMA21:%.2f EMA50:%.2f EMA200:%.2f | BBmid:%.2f",
            trend_status, vrsi, price, v21, v50, v200, vbb_mid
        )
        return None

    score = 0
    reasons = []
    sl_dist = vatr * ATR_MULT

    # ── BULLISH SCORING ───────────────────────────────────────────────────────
    if bull_trend_ok:
        # 1. Trend
        score += 1
        reasons.append("Trend bullish: price above 200 EMA and EMA21 > EMA50")

        # 2. Price above 50 EMA
        if price > v50:
            score += 1
            reasons.append(f"Price above 50 EMA ({v50:.2f})")

        # 3. RSI bullish
        if vrsi > 55:
            score += 1
            reasons.append(f"RSI bullish zone ({vrsi:.1f})")

        # 4. RSI not stretched
        if 55 < vrsi < 72:
            score += 1
            reasons.append("RSI has room — not yet overbought")

        # 5. MACD bullish
        if vmh > 0 and vml > vms:
            score += 1
            reasons.append("MACD bullish confirmation")

        # 6. Stoch RSI bullish
        if kval is not None and dval is not None and kval > dval and kval > 50:
            score += 1
            reasons.append(f"Stoch RSI bullish crossover/strength ({kval:.1f})")

        # 7. Bollinger Bands location
        # Prefer price not chasing above upper band.
        # Bonus if price is between middle band and upper band, or slightly above middle band.
        if price <= vbb_upper:
            score += 1
            if price >= vbb_mid:
                reasons.append("Bollinger location supportive: price above mid-band but not overextended")
            else:
                reasons.append("Bollinger location acceptable: price not overextended above upper band")

        log.info(
            "Score -> BUY:%d/7 | Trend:%s | RSI:%.1f | Price:%.2f | EMA21:%.2f EMA50:%.2f EMA200:%.2f | BBmid:%.2f BBup:%.2f BBlow:%.2f",
            score, trend_status, vrsi, price, v21, v50, v200, vbb_mid, vbb_upper, vbb_lower
        )

        if score >= THRESHOLD:
            entry = price
            return {
                "direction": "BUY",
                "emoji": "🟢",
                "entry": entry,
                "sl": round(entry - sl_dist, 2),
                "tp1": round(entry + sl_dist * TP1_RATIO, 2),
                "tp2": round(entry + sl_dist * TP2_RATIO, 2),
                "score": score,
                "max_score": 7,
                "reasons": reasons,
                "atr": round(vatr, 2),
                "rsi": round(vrsi, 1),
                "bb_mid": round(vbb_mid, 2),
                "bb_upper": round(vbb_upper, 2),
                "bb_lower": round(vbb_lower, 2),
            }

    # ── BEARISH SCORING ───────────────────────────────────────────────────────
    if bear_trend_ok:
        # 1. Trend
        score += 1
        reasons.append("Trend bearish: price below 200 EMA and EMA21 < EMA50")

        # 2. Price below 50 EMA
        if price < v50:
            score += 1
            reasons.append(f"Price below 50 EMA ({v50:.2f})")

        # 3. RSI bearish
        if vrsi < 45:
            score += 1
            reasons.append(f"RSI bearish zone ({vrsi:.1f})")

        # 4. RSI not stretched
        if 28 < vrsi < 45:
            score += 1
            reasons.append("RSI has room — not yet oversold")

        # 5. MACD bearish
        if vmh < 0 and vml < vms:
            score += 1
            reasons.append("MACD bearish confirmation")

        # 6. Stoch RSI bearish
        if kval is not None and dval is not None and kval < dval and kval < 50:
            score += 1
            reasons.append(f"Stoch RSI bearish crossover/strength ({kval:.1f})")

        # 7. Bollinger Bands location
        # Prefer price not chasing below lower band.
        # Bonus if price is between lower band and middle band.
        if price >= vbb_lower:
            score += 1
            if price <= vbb_mid:
                reasons.append("Bollinger location supportive: price below mid-band but not overextended")
            else:
                reasons.append("Bollinger location acceptable: price not overextended below lower band")

        log.info(
            "Score -> SELL:%d/7 | Trend:%s | RSI:%.1f | Price:%.2f | EMA21:%.2f EMA50:%.2f EMA200:%.2f | BBmid:%.2f BBup:%.2f BBlow:%.2f",
            score, trend_status, vrsi, price, v21, v50, v200, vbb_mid, vbb_upper, vbb_lower
        )

        if score >= THRESHOLD:
            entry = price
            return {
                "direction": "SELL",
                "emoji": "🔴",
                "entry": entry,
                "sl": round(entry + sl_dist, 2),
                "tp1": round(entry - sl_dist * TP1_RATIO, 2),
                "tp2": round(entry - sl_dist * TP2_RATIO, 2),
                "score": score,
                "max_score": 7,
                "reasons": reasons,
                "atr": round(vatr, 2),
                "rsi": round(vrsi, 1),
                "bb_mid": round(vbb_mid, 2),
                "bb_upper": round(vbb_upper, 2),
                "bb_lower": round(vbb_lower, 2),
            }

    return None

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False


def format_signal(sig, ts):
    reasons = "\n".join(f"  • {r}" for r in sig["reasons"])
    filled = "█" * sig["score"]
    empty = "░" * (sig["max_score"] - sig["score"])
    direction = f"{sig['emoji']} <b>{sig['direction']}</b>"

    return (
        f"<b>⚡ XAUUSD SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair :</b>      XAU/USD  (Gold)\n"
        f"<b>Signal :</b>   {direction}\n"
        f"<b>Entry :</b>    <code>{sig['entry']:.2f}</code>\n"
        f"<b>TP 1 :</b>     <code>{sig['tp1']:.2f}</code>\n"
        f"<b>TP 2 :</b>     <code>{sig['tp2']:.2f}</code>\n"
        f"<b>Stop Loss :</b> <code>{sig['sl']:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Confluence :</b> {filled}{empty}  {sig['score']}/{sig['max_score']}\n"
        f"<b>RSI :</b> {sig['rsi']}    <b>ATR :</b> {sig['atr']}\n"
        f"<b>BB Mid :</b> <code>{sig['bb_mid']}</code>    <b>BB Upper :</b> <code>{sig['bb_upper']}</code>\n"
        f"<b>BB Lower :</b> <code>{sig['bb_lower']}</code>\n"
        f"\n<b>📊 Analysis:</b>\n{reasons}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🕐 {ts} UTC  |  Timeframe: 15m</i>\n"
        f"<i>⚠️ Educational purposes only. Manage your risk.</i>"
    )

# ─── DUPLICATE GUARD ───────────────────────────────────────────────────────────
_last = {"direction": None, "time": None}

def is_dup(sig, ts):
    return _last["direction"] == sig["direction"] and _last["time"] == ts

# ─── SCAN COUNTER ──────────────────────────────────────────────────────────────
_scan_count = 0
_last_hb_date = None

# ─── TIMING HELPER ─────────────────────────────────────────────────────────────

def sleep_until_next_15m():
    now = datetime.now(timezone.utc)
    next_minute = ((now.minute // 15) + 1) * 15

    if next_minute == 60:
        next_run = now.replace(minute=0, second=5, microsecond=0) + timedelta(hours=1)
    else:
        next_run = now.replace(minute=next_minute, second=5, microsecond=0)

    sleep_seconds = (next_run - now).total_seconds()
    if sleep_seconds > 0:
        log.info("Sleeping %.0f seconds until next 15m candle close...", sleep_seconds)
        time.sleep(sleep_seconds)

# ─── BOT LOOP ──────────────────────────────────────────────────────────────────

def bot_loop():
    global _scan_count, _last_hb_date

    log.info("XAUUSD Signal Bot started")

    send_telegram(
        "🤖 <b>XAUUSD Signal Bot Online</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 Pair: XAU/USD  |  Timeframe: 15m\n"
        "🔍 Scanning on each 15-minute candle close\n"
        "⚙️ Trend-following mode with Bollinger Band entry filter enabled\n"
        "📦 Data bars loaded: 250 (EMA200-ready)\n"
        "💬 Daily status update sent every morning at 08:00 UTC\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Indicators: EMA21/50/200 · RSI · MACD · StochRSI · Bollinger Bands · ATR</i>"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")

            if now.hour == 8 and now.minute < 15 and _last_hb_date != today_str:
                candles_hb = fetch_candles()
                latest_price = candles_hb[0]["close"] if candles_hb else 0.0

                send_telegram(
                    f"🟡 <b>Daily Status — {today_str}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Bot is alive and scanning\n"
                    f"💰 XAU/USD current price: <code>{latest_price:.2f}</code>\n"
                    f"🔍 Scans completed today: {_scan_count}\n"
                    f"⚙️ Threshold: {THRESHOLD}/7  |  TF: 15m\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Next signal fires when trend, momentum and Bollinger location align</i>"
                )
                _last_hb_date = today_str
                _scan_count = 0

            log.info("Scanning… (scan #%d today)", _scan_count + 1)
            candles = fetch_candles()

            if candles is None:
                log.warning("No data — retry in 60s")
                send_telegram("⚠️ <b>Warning:</b> Failed to fetch XAU/USD data. Retrying in 60s.")
                time.sleep(60)
                continue

            _scan_count += 1
            ts = candles[0]["time"]
            price = candles[0]["close"]
            log.info("Candle [%s]  close=%.2f", ts, price)

            sig = analyse(candles)
            if sig:
                if not is_dup(sig, ts):
                    msg = format_signal(sig, ts)
                    sent = send_telegram(msg)
                    if sent:
                        log.info(
                            "Signal sent: %s @ %.2f  score=%d/%d",
                            sig["direction"], sig["entry"], sig["score"], sig["max_score"]
                        )
                        _last.update({"direction": sig["direction"], "time": ts})
                else:
                    log.info("Duplicate signal skipped.")
            else:
                log.info("No signal fired. Check score log above for details.")

        except Exception as e:
            log.error("Loop error: %s", e)
            send_telegram(f"🔴 <b>Bot error:</b> <code>{str(e)[:200]}</code>")

        sleep_until_next_15m()

# ─── ENTRY ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    bot_loop()