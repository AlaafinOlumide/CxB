import threading
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from config import settings
from twelvedata_client import fetch_ohlc
from strategy import build_signal, format_signal
from telegram_client import send_telegram

app = Flask(__name__)
state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_run": None,
    "last_signal": None,
    "last_sent_signature": None,
    "last_sent_at": None,
    "errors": [],
}


def should_send(sig) -> bool:
    if sig.direction == "WAIT" and not settings.SEND_WAIT_SIGNALS:
        return False
    if sig.signature == state.get("last_sent_signature"):
        return False
    last_sent_at = state.get("last_sent_at")
    if last_sent_at:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_sent_at)
        if elapsed < timedelta(minutes=settings.SIGNAL_COOLDOWN_MINUTES):
            return False
    return True


def run_once():
    m5 = fetch_ohlc("5min")
    m15 = fetch_ohlc("15min")
    sig = build_signal(m5, m15)
    msg = format_signal(sig)

    now = datetime.now(timezone.utc).isoformat()
    state["last_run"] = now
    state["last_signal"] = {
        "direction": sig.direction,
        "confidence": sig.confidence,
        "signature": sig.signature,
        "message": msg,
    }

    if should_send(sig):
        send_telegram(msg)
        state["last_sent_signature"] = sig.signature
        state["last_sent_at"] = now

    print(f"[{now}] {sig.direction} {sig.confidence} {sig.signature}")


def worker():
    if settings.SEND_STARTUP_MESSAGE:
        send_telegram("✅ XAUUSD signal bot started. Data: Twelve Data. Mode: Telegram signals only.")
    while True:
        try:
            run_once()
        except Exception as e:
            err = f"{datetime.now(timezone.utc).isoformat()} - {type(e).__name__}: {e}"
            print(err)
            state["errors"] = (state.get("errors", []) + [err])[-10:]
        time.sleep(settings.POLL_SECONDS)


@app.route("/")
def home():
    return "XAUUSD Signal Bot is running. Use /health or /last"


@app.route("/health")
def health():
    return jsonify({"ok": True, **state})


@app.route("/last")
def last():
    return jsonify(state.get("last_signal") or {})


if __name__ == "__main__":
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=settings.PORT)
