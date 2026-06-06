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
    "worker_started": False,
    "errors": [],
}
_worker_lock = threading.Lock()


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


def run_once(send=True):
    print("Fetching XAUUSD M5 and M15 closed candles...")
    m5 = fetch_ohlc("5min")
    m15 = fetch_ohlc("15min")
    sig = build_signal(m5, m15)
    msg = format_signal(sig)

    now = datetime.now(timezone.utc).isoformat()
    state["last_run"] = now
    state["last_signal"] = {
        "direction": sig.direction,
        "confidence": sig.confidence,
        "score": sig.score,
        "signature": sig.signature,
        "message": msg,
    }

    if send and should_send(sig):
        send_telegram(msg)
        state["last_sent_signature"] = sig.signature
        state["last_sent_at"] = now
        print(f"Telegram sent: {sig.direction} {sig.confidence} {sig.signature}")
    else:
        print(f"No Telegram send: {sig.direction} {sig.confidence} {sig.signature}")
    return sig


def worker():
    if settings.SEND_STARTUP_MESSAGE:
        send_telegram("✅ XAUUSD signal bot started. Format: Best Scenario, Entry, TP, SL, Key Points, Bias, Invalidation. Using closed M5/M15 candles only.")
    while True:
        try:
            run_once(send=True)
        except Exception as e:
            err = f"{datetime.now(timezone.utc).isoformat()} - {type(e).__name__}: {e}"
            print(err)
            state["errors"] = (state.get("errors", []) + [err])[-10:]
            if settings.SEND_ERROR_MESSAGES:
                try:
                    send_telegram(f"⚠️ XAUUSD bot error:\n{err}")
                except Exception:
                    pass
        time.sleep(settings.POLL_SECONDS)


def start_worker_once():
    with _worker_lock:
        if not state["worker_started"]:
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            state["worker_started"] = True
            print("Background signal worker started")


@app.route("/")
def home():
    return "XAUUSD Signal Bot is running. Use /health, /last, /run-once, /test-telegram"


@app.route("/health")
def health():
    return jsonify({"ok": True, **state})


@app.route("/last")
def last():
    return jsonify(state.get("last_signal") or {})


@app.route("/run-once")
def run_once_route():
    try:
        sig = run_once(send=True)
        return jsonify({"ok": True, "direction": sig.direction, "confidence": sig.confidence, "score": sig.score})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/test-telegram")
def test_telegram():
    send_telegram("✅ Telegram test successful. XAUUSD signal bot is connected.")
    return jsonify({"ok": True, "message": "Telegram test sent"})


# Important: Gunicorn imports this module, so start the worker on import.
start_worker_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=settings.PORT)
