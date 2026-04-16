"""
XAUUSD Signal Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Data      : TwelveData  (free: 8 req/min, 800/day)
Signals   : Telegram
Hosting   : Render (free web service + keep-alive)
Strategy  : Multi-confluence scoring
            EMA trend stack + RSI + MACD + StochRSI + ATR
            Signal fires at >= 4/8 with mandatory EMA gate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TWELVE_API_KEY   = os.getenv("TWELVE_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL      = "XAU/USD"
INTERVAL    = "15min"
SCAN_EVERY  = 60 * 15        # every 15 minutes
CANDLES     = 100

TP1_RATIO   = 1.5
TP2_RATIO   = 3.0
ATR_MULT    = 1.2
THRESHOLD   = 4              # lowered to 4/8 — EMA gate still mandatory

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
        "symbol":     SYMBOL,
        "interval":   INTERVAL,
        "outputsize": CANDLES,
        "apikey":     TWELVE_API_KEY,
        "format":     "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "values" not in data:
            log.error("TwelveData error: %s", data.get("message", data))
            return None
        return [
            {
                "time":  c["datetime"],
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
            }
            for c in data["values"]
        ]
    except Exception as e:
        log.error("fetch_candles error: %s", e)
        return None

# ─── INDICATORS ────────────────────────────────────────────────────────────────

def ema(values, period):
    k      = 2 / (period + 1)
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
            d  = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(d, 0)) / period
            al = (al * (period - 1) + max(-d, 0)) / period
        rs     = ag / al if al != 0 else 100
        out[i] = 100 - (100 / (1 + rs))
    return out


def macd(closes, fast=12, slow=26, sig=9):
    ef   = ema(closes, fast)
    es   = ema(closes, slow)
    line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ef, es)
    ]
    valid = [v for v in line if v is not None]
    sig_r = ema(valid, sig)
    offset = next(i for i, v in enumerate(line) if v is not None)
    sig_f  = [None] * len(line)
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
        out  = [None] * len(arr)
        vals = [(i, v) for i, v in enumerate(arr) if v is not None]
        for idx in range(p - 1, len(vals)):
            avg = sum(vals[j][1] for j in range(idx - p + 1, idx + 1)) / p
            out[vals[idx][0]] = avg
        return out

    k_line = smooth(stoch, smooth_k)
    d_line = smooth(k_line, smooth_d)
    return k_line, d_line

# ─── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def analyse(candles):
    if len(candles) < 60:
        return None

    c      = list(reversed(candles))
    closes = [x["close"] for x in c]
    n      = len(closes)
    i      = n - 1

    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e200 = ema(closes, 200)
    rsi_ = rsi(closes, 14)
    ml, ms, mh = macd(closes)
    atr_ = atr(c, 14)
    sk, _ = stoch_rsi(rsi_)

    price = closes[i]
    vals  = [e21[i], e50[i], e200[i], rsi_[i], ml[i], ms[i], mh[i], atr_[i]]
    if any(v is None for v in vals):
        return None

    v21, v50, v200, vrsi, vml, vms, vmh, vatr = vals
    kval = sk[i]

    sb, se = 0, 0
    rb, re = [], []

    # 1. EMA stack (2pts) — also used as mandatory gate
    if v21 > v50 > v200:
        sb += 2; rb.append("EMA stack bullish (21 > 50 > 200)")
    elif v21 < v50 < v200:
        se += 2; re.append("EMA stack bearish (21 < 50 < 200)")

    # 2. Price vs 50 EMA (1pt)
    if price > v50:
        sb += 1; rb.append(f"Price above 50 EMA ({v50:.2f})")
    else:
        se += 1; re.append(f"Price below 50 EMA ({v50:.2f})")

    # 3. RSI direction (1pt)
    if vrsi > 55:
        sb += 1; rb.append(f"RSI bullish zone ({vrsi:.1f})")
    elif vrsi < 45:
        se += 1; re.append(f"RSI bearish zone ({vrsi:.1f})")

    # 4. RSI room to run (1pt)
    if 55 < vrsi < 70:
        sb += 1; rb.append("RSI has room — not yet overbought")
    elif 30 < vrsi < 45:
        se += 1; re.append("RSI has room — not yet oversold")

    # 5. MACD histogram (1pt)
    if vmh > 0:
        sb += 1; rb.append("MACD histogram positive")
    elif vmh < 0:
        se += 1; re.append("MACD histogram negative")

    # 6. MACD line vs signal (1pt)
    if vml > vms:
        sb += 1; rb.append("MACD line above signal line")
    elif vml < vms:
        se += 1; re.append("MACD line below signal line")

    # 7. Stoch RSI (1pt)
    if kval is not None:
        if kval > 55:
            sb += 1; rb.append(f"Stoch RSI bullish ({kval:.1f})")
        elif kval < 45:
            se += 1; re.append(f"Stoch RSI bearish ({kval:.1f})")

    sl_dist = vatr * ATR_MULT

    # Mandatory gate: EMA stack must be aligned regardless of score
    bull_ema_aligned = v21 > v50 > v200
    bear_ema_aligned = v21 < v50 < v200

    if sb >= THRESHOLD and sb > se and bull_ema_aligned:
        entry = price
        return {
            "direction": "BUY",
            "emoji":     "🟢",
            "entry":     entry,
            "sl":        round(entry - sl_dist, 2),
            "tp1":       round(entry + sl_dist * TP1_RATIO, 2),
            "tp2":       round(entry + sl_dist * TP2_RATIO, 2),
            "score":     sb,
            "reasons":   rb,
            "atr":       round(vatr, 2),
            "rsi":       round(vrsi, 1),
        }

    if se >= THRESHOLD and se > sb and bear_ema_aligned:
        entry = price
        return {
            "direction": "SELL",
            "emoji":     "🔴",
            "entry":     entry,
            "sl":        round(entry + sl_dist, 2),
            "tp1":       round(entry - sl_dist * TP1_RATIO, 2),
            "tp2":       round(entry - sl_dist * TP2_RATIO, 2),
            "score":     se,
            "reasons":   re,
            "atr":       round(vatr, 2),
            "rsi":       round(vrsi, 1),
        }

    return None

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False


def format_signal(sig, ts):
    reasons   = "\n".join(f"  • {r}" for r in sig["reasons"])
    filled    = "█" * sig["score"]
    empty     = "░" * (8 - sig["score"])
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
        f"<b>Confluence :</b> {filled}{empty}  {sig['score']}/8\n"
        f"<b>RSI :</b> {sig['rsi']}    <b>ATR :</b> {sig['atr']}\n"
        f"\n<b>📊 Analysis:</b>\n{reasons}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🕐 {ts} UTC  |  Timeframe: 15m</i>\n"
        f"<i>⚠️ Educational purposes only. Manage your risk.</i>"
    )

# ─── DUPLICATE GUARD ───────────────────────────────────────────────────────────
_last = {"direction": None, "time": None}

def is_dup(sig, ts):
    return _last["direction"] == sig["direction"] and _last["time"] == ts

# ─── SCAN COUNTER (for daily heartbeat) ───────────────────────────────────────
_scan_count   = 0
_last_hb_date = None

# ─── BOT LOOP ──────────────────────────────────────────────────────────────────

def bot_loop():
    global _scan_count, _last_hb_date

    log.info("XAUUSD Signal Bot started")
    send_telegram(
        "🤖 <b>XAUUSD Signal Bot Online</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 Pair: XAU/USD  |  Timeframe: 15m\n"
        "🔍 Scanning every 15 minutes\n"
        "⚙️ Signals require 4/8 confluence (EMA gate mandatory)\n"
        "💬 Daily status update sent every morning at 08:00 UTC\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Indicators: EMA21/50/200 · RSI · MACD · StochRSI · ATR</i>"
    )

    while True:
        try:
            now       = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")

            # ── Daily heartbeat at 08:00 UTC ──────────────────────────────────
            if now.hour == 8 and now.minute < 15 and _last_hb_date != today_str:
                candles_hb = fetch_candles()
                price_hb   = candles_hb[0]["close"] if candles_hb else 0.0
                send_telegram(
                    f"🟡 <b>Daily Status — {today_str}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Bot is alive and scanning\n"
                    f"💰 XAU/USD current price: <code>{price_hb:.2f}</code>\n"
                    f"🔍 Scans completed today: {_scan_count}\n"
                    f"⚙️ Threshold: {THRESHOLD}/8  |  TF: 15m\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Next signal fires when 4+ confluence factors align with EMA trend</i>"
                )
                _last_hb_date = today_str
                _scan_count   = 0   # reset daily counter

            # ── Main scan ─────────────────────────────────────────────────────
            log.info("Scanning… (scan #%d today)", _scan_count + 1)
            candles = fetch_candles()

            if candles is None:
                log.warning("No data — retry in 60s")
                send_telegram("⚠️ <b>Warning:</b> Failed to fetch XAU/USD data. Retrying in 60s.")
                time.sleep(60)
                continue

            _scan_count += 1
            ts    = candles[0]["time"]
            price = candles[0]["close"]
            log.info("Candle [%s]  close=%.2f", ts, price)

            sig = analyse(candles)
            if sig:
                if not is_dup(sig, ts):
                    msg  = format_signal(sig, ts)
                    sent = send_telegram(msg)
                    if sent:
                        log.info("Signal sent: %s @ %.2f  score=%d/8",
                                 sig["direction"], sig["entry"], sig["score"])
                        _last.update({"direction": sig["direction"], "time": ts})
                else:
                    log.info("Duplicate signal skipped.")
            else:
                log.info("No setup — score below %d/8 or EMA not aligned.", THRESHOLD)

        except Exception as e:
            log.error("Loop error: %s", e)
            send_telegram(f"🔴 <b>Bot error:</b> <code>{str(e)[:200]}</code>")

        time.sleep(SCAN_EVERY)


# ─── ENTRY ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    bot_loop()
