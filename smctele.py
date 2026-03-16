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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()   # optional now
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
INTERVAL = os.getenv("INTERVAL", "1min")
SOURCE_TIMEZONE = os.getenv("SOURCE_TIMEZONE", "UTC")
OUTPUT_TIMEZONE = os.getenv("OUTPUT_TIMEZONE", "Asia/Kolkata")

DEFAULT_RR = float(os.getenv("DEFAULT_RR", "2.0"))
MIN_SL_DISTANCE = float(os.getenv("MIN_SL_DISTANCE", "0.20"))
SL_BUFFER = float(os.getenv("SL_BUFFER", "0.03"))

LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "300"))
SWING_WINDOW = int(os.getenv("SWING_WINDOW", "2"))
OB_LOOKBACK = int(os.getenv("OB_LOOKBACK", "8"))
ENTRY_WAIT_BARS = int(os.getenv("ENTRY_WAIT_BARS", "20"))
MAX_TRADE_BARS = int(os.getenv("MAX_TRADE_BARS", "180"))

POLL_SLEEP_SECONDS = int(os.getenv("POLL_SLEEP_SECONDS", "2"))
SIGNAL_CHECK_SECONDS = int(os.getenv("SIGNAL_CHECK_SECONDS", "60"))

AUTO_START = os.getenv("AUTO_START", "true").strip().lower() == "true"

# =========================================
# SIMPLE STATE
# =========================================
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "bot_enabled": AUTO_START,
    "rr": DEFAULT_RR,
    "last_signal_id": None,
    "pending_signal": None,
    "telegram_offset": 0,
    "last_checked_ts": 0.0,
    "chat_id": TELEGRAM_CHAT_ID,   # auto-detect if empty
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
            "bot_enabled": state["bot_enabled"],
            "rr": state["rr"],
            "has_pending_signal": state["pending_signal"] is not None,
            "last_signal_id": state["last_signal_id"],
            "chat_id_known": bool(state["chat_id"]),
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
            "chat_id_known": bool(state["chat_id"]),
        })


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
    resp = requests.post(url, json={"drop_pending_updates": False}, timeout=20)
    resp.raise_for_status()
    print("[bot] deleteWebhook response:", resp.json())


def send_telegram_message(text: str) -> None:
    with state_lock:
        chat_id = state["chat_id"]

    if not chat_id:
        print("[bot] chat_id unknown, message skipped")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        tg_api("sendMessage", payload, timeout=20)
        print(f"[bot] message sent to chat_id={chat_id}")
    except Exception as e:
        print(f"[Telegram send error] {e}")


def get_updates(offset: int) -> List[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    payload = {
        "offset": offset,
        "timeout": 25,
        "allowed_updates": ["message"],
    }
    resp = requests.post(url, json=payload, timeout=35)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {data}")
    return data.get("result", [])


# =========================================
# TWELVE DATA
# =========================================
def get_twelvedata_candles(symbol: str, interval: str, outputsize: int) -> List[Candle]:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": SOURCE_TIMEZONE,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    }
    resp = requests