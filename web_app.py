import os
import time
import json
import threading
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# =========================================
# CONFIG FROM ENV
# =========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # optional, can be blank
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
INTERVAL = os.getenv("INTERVAL", "1min")
SOURCE_TIMEZONE = os.getenv("SOURCE_TIMEZONE", "UTC")
OUTPUT_TIMEZONE = os.getenv("OUTPUT_TIMEZONE", "Asia/Kolkata")

DEFAULT_RR = float(os.getenv("DEFAULT_RR", "2.0"))
MIN_SL_DISTANCE = float(os.getenv("MIN_SL_DISTANCE", "0.20"))
SL_BUFFER = float(os.getenv("SL_BUFFER", "0.03"))

LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "120"))
SWING_WINDOW = int(os.getenv("SWING_WINDOW", "2"))
OB_LOOKBACK = int(os.getenv("OB_LOOKBACK", "8"))
ENTRY_WAIT_BARS = int(os.getenv("ENTRY_WAIT_BARS", "20"))
MAX_TRADE_BARS = int(os.getenv("MAX_TRADE_BARS", "180"))

POLL_SLEEP_SECONDS = int(os.getenv("POLL_SLEEP_SECONDS", "2"))
SIGNAL_CHECK_SECONDS = int(os.getenv("SIGNAL_CHECK_SECONDS", "300"))

AUTO_START_BOT = os.getenv("AUTO_START_BOT", "true").strip().lower() == "true"
AUTO_CAPTURE_CHAT_ID = os.getenv("AUTO_CAPTURE_CHAT_ID", "true").strip().lower() == "true"
SEND_RESTART_MESSAGE = os.getenv("SEND_RESTART_MESSAGE", "true").strip().lower() == "true"

# =========================================
# SIMPLE STATE
# =========================================
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "bot_enabled": AUTO_START_BOT,
    "rr": DEFAULT_RR,
    "last_signal_id": None,
    "pending_signal": None,
    "telegram_offset": 0,
    "detected_chat_id": TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else None,
    "startup_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    "last_candle_fetch_count": 0,
    "last_closed_candle": None,
    "last_signal_scan_time_utc": None,
    "last_error": None,
    "api_credit_notified": False,
    "api_resumed_notified": False,
    "last_wake_http_utc": None,
    "last_command_text": None,
    "last_command_time_utc": None,
}

# =========================================
# APP
# =========================================
app = Flask(__name__)


@app.get("/")
def home():
    with state_lock:
        return jsonify({
            "ok": True,
            "service": "xauusd_smc_bot",
            "message": "Service is awake. On Render free, open this URL first, then wait 30-60 seconds before sending Telegram commands.",
            "bot_enabled": state["bot_enabled"],
            "rr": state["rr"],
            "has_pending_signal": state["pending_signal"] is not None,
            "last_signal_id": state["last_signal_id"],
            "detected_chat_id": state["detected_chat_id"],
            "startup_time_utc": state["startup_time_utc"],
            "last_wake_http_utc": state["last_wake_http_utc"],
        })


@app.get("/wake")
def wake():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with state_lock:
        state["last_wake_http_utc"] = now
    return jsonify({
        "ok": True,
        "message": "Wake request received. If this service had spun down on Render free, give it about 30-60 seconds, then send your Telegram command.",
        "wake_time_utc": now,
    })


@app.get("/health")
def health():
    with state_lock:
        return jsonify({
            "ok": True,
            "service": "xauusd_smc_bot",
            "bot_enabled": state["bot_enabled"],
            "rr": state["rr"],
            "pending_signal": state["pending_signal"] is not None,
            "last_signal_id": state["last_signal_id"],
            "telegram_offset": state["telegram_offset"],
            "detected_chat_id": state["detected_chat_id"],
            "last_candle_fetch_count": state["last_candle_fetch_count"],
            "last_closed_candle": state["last_closed_candle"],
            "last_signal_scan_time_utc": state["last_signal_scan_time_utc"],
            "last_error": state["last_error"],
            "api_credit_notified": state["api_credit_notified"],
            "api_resumed_notified": state["api_resumed_notified"],
            "last_command_text": state["last_command_text"],
            "last_command_time_utc": state["last_command_time_utc"],
        })


@app.get("/ping")
def ping():
    return jsonify({"ok": True, "message": "pong"})


@app.get("/debug/state")
def debug_state():
    with state_lock:
        return jsonify({"ok": True, "state": state})


@app.get("/debug/telegram")
def debug_telegram():
    with state_lock:
        return jsonify({
            "ok": True,
            "telegram_offset": state["telegram_offset"],
            "detected_chat_id": state["detected_chat_id"],
            "bot_enabled": state["bot_enabled"],
            "last_error": state["last_error"],
            "last_command_text": state["last_command_text"],
            "last_command_time_utc": state["last_command_time_utc"],
        })


@app.get("/debug/candles")
def debug_candles():
    try:
        rows = get_twelvedata_candles(SYMBOL, INTERVAL, 5)
        return jsonify({
            "ok": True,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "count": len(rows),
            "latest": asdict(rows[-1]) if rows else None,
            "previous": asdict(rows[-2]) if len(rows) >= 2 else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/debug/signal")
def debug_signal():
    try:
        rows = get_twelvedata_candles(SYMBOL, INTERVAL, LOOKBACK_BARS)
        with state_lock:
            rr = float(state["rr"])
            enabled = state["bot_enabled"]
        signal = detect_smc_signal(rows, rr)
        return jsonify({
            "ok": True,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "rows": len(rows),
            "bot_enabled": enabled,
            "signal_found": signal is not None,
            "signal": asdict(signal) if signal else None,
            "last_closed_candle": asdict(rows[-2]) if len(rows) >= 2 else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/debug/sendtest")
def debug_sendtest():
    try:
        send_telegram_message("✅ Debug route test message from Render")
        return jsonify({"ok": True, "message": "test sent"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================
# DATA MODELS
# =========================================
@dataclass
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Signal:
    signal_id: str
    direction: str
    bos_time: str
    ob_time: str
    zone_low: float
    zone_high: float
    entry: float
    sl: float
    tp: float
    risk: float
    expires_at_epoch: float
    sent_entry_alert: bool
    is_active_trade: bool
    entry_time: Optional[str]


# =========================================
# HELPERS
# =========================================
def set_last_error(msg: Optional[str]) -> None:
    with state_lock:
        state["last_error"] = msg


def require_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def round5(x: float) -> float:
    return round(float(x), 5)


def parse_twelvedata_datetime_utc(dt_str: str) -> float:
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def get_effective_chat_id() -> Optional[str]:
    with state_lock:
        return state.get("detected_chat_id")


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def is_credit_error(exc: Exception) -> bool:
    text = str(exc)
    return ("429" in text) or ("API credits" in text) or ("run out of API credits" in text)


# =========================================
# TELEGRAM
# =========================================
def tg_api(method: str, payload: Optional[dict] = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload or {}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def clear_telegram_webhook() -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    resp = requests.post(url, json={"drop_pending