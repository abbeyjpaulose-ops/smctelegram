from __future__ import annotations

import os
import json
import time
import math
import threading
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import requests
from flask import Flask, jsonify

# =========================================================
# ENV / CONFIG
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

# Optional fixed chat id. If empty, first /startbot chat will be stored in state.
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD").strip()
INPUT_TIMEZONE = os.getenv("INPUT_TIMEZONE", "UTC").strip()
OUTPUT_TIMEZONE = os.getenv("OUTPUT_TIMEZONE", "Asia/Kolkata").strip()

RR = float(os.getenv("RR", "2.0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
PORT = int(os.getenv("PORT", "10000"))

# SMC logic
SMC_ENTRY_TF = "15min"
SMC_SWING_WINDOW = int(os.getenv("SMC_SWING_WINDOW", "2"))
SMC_OB_SEARCH_BACK = int(os.getenv("SMC_OB_SEARCH_BACK", "8"))
SMC_OB_USE_BODY = os.getenv("SMC_OB_USE_BODY", "true").strip().lower() == "true"

ENTRY_WAIT_BARS_SMC = int(os.getenv("ENTRY_WAIT_BARS_SMC", "180"))   # 180 x 1min = 3h
MAX_HOLD_BARS = int(os.getenv("MAX_HOLD_BARS", str(24 * 60)))        # 24h
MIN_STOP_DISTANCE = float(os.getenv("MIN_STOP_DISTANCE", "0.20"))
PRICE_BUFFER = float(os.getenv("PRICE_BUFFER", "0.05"))
MIN_SL_FILTER = float(os.getenv("MIN_SL_FILTER", "0.49"))
ONE_TRADE_AT_A_TIME = os.getenv("ONE_TRADE_AT_A_TIME", "true").strip().lower() == "true"
CONSERVATIVE_SAME_CANDLE = os.getenv("CONSERVATIVE_SAME_CANDLE", "true").strip().lower() == "true"

# Data fetch sizes
TD_OUTPUTSIZE_1M = int(os.getenv("TD_OUTPUTSIZE_1M", "2000"))
TD_OUTPUTSIZE_15M = int(os.getenv("TD_OUTPUTSIZE_15M", "500"))

STATE_PATH = os.getenv("STATE_PATH", "bot_state.json")

# =========================================================
# VALIDATION
# =========================================================
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not TWELVEDATA_API_KEY:
    raise RuntimeError("Missing TWELVEDATA_API_KEY")


# =========================================================
# FLASK APP FOR RENDER HEALTH
# =========================================================
app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({"ok": True, "service": "xauusd_smc_bot"})


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "xauusd_smc_bot"})


# =========================================================
# TELEGRAM HELPERS
# =========================================================
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def tg_api(method: str, payload: Optional[dict] = None, timeout: int = 30) -> dict:
    url = f"{TG_BASE}/{method}"
    resp = requests.post(url, data=payload or {}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def telegram_delete_webhook():
    try:
        tg_api("deleteWebhook", {"drop_pending_updates": False}, timeout=30)
    except Exception as e:
        print(f"deleteWebhook warning: {e}", flush=True)


def send_telegram_message(text: str, chat_id: Optional[str]) -> None:
    if not chat_id:
        print("send_telegram_message skipped: no chat_id configured yet.", flush=True)
        return
    try:
        tg_api("sendMessage", {"chat_id": str(chat_id), "text": text}, timeout=30)
    except Exception as e:
        print(f"sendMessage error: {e}", flush=True)


def get_updates(offset: Optional[int], timeout_sec: int = 25) -> List[dict]:
    payload = {
        "timeout": timeout_sec,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        payload["offset"] = offset
    data = tg_api("getUpdates", payload, timeout=timeout_sec + 10)
    return data.get("result", [])


def normalize_command(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("/"):
        return ""
    cmd = text.split()[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd


# =========================================================
# TWELVE DATA HELPERS
# =========================================================
def td_time_series(symbol: str, interval: str, outputsize: int) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "timezone": "UTC",
        "order": "ASC",
        "apikey": TWELVEDATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=40)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {data}")

    values = data.get("values", [])
    if not values:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])

    df = pd.DataFrame(values).copy()

    # Twelve Data usually returns 'datetime'
    time_col = "datetime" if "datetime" in df.columns else "timestamp"
    df = df[[time_col, "open", "high", "low", "close"]].copy()
    df.columns = ["timestamp", "open", "high", "low", "close"]

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"]).copy()
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

    # Basic candle sanity
    df = df[
        (df["high"] >= df["low"]) &
        (df["high"] >= df["open"]) &
        (df["high"] >= df["close"]) &
        (df["low"] <= df["open"]) &
        (df["low"] <= df["close"])
    ].copy()

    return df.reset_index(drop=True)


# =========================================================
# STATE
# =========================================================
@dataclass
class PendingSignal:
    bos_key: str
    direction: str
    setup_time: str           # ISO UTC
    entry: float
    sl: float
    tp: float
    sl_distance: float
    ob_time: str              # ISO UTC
    ob_zone_low: float
    ob_zone_high: float
    expires_at: str           # ISO UTC
    signal_sent: bool = False
    entry_taken: bool = False
    entry_time: Optional[str] = None
    result_sent: bool = False
    status: str = "waiting_entry"   # waiting_entry / active / closed / expired


@dataclass
class BotState:
    bot_enabled: bool = False
    chat_id: Optional[str] = None
    telegram_offset: Optional[int] = None
    used_bos_keys: Optional[List[str]] = None
    active_trade: Optional[Dict[str, Any]] = None
    pending_signal: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.used_bos_keys is None:
            self.used_bos_keys = []


def load_state() -> BotState:
    if not os.path.exists(STATE_PATH):
        return BotState(chat_id=TELEGRAM_CHAT_ID or None)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        state = BotState(**raw)
        if not state.chat_id and TELEGRAM_CHAT_ID:
            state.chat_id = TELEGRAM_CHAT_ID
        return state
    except Exception as e:
        print(f"load_state warning: {e}", flush=True)
        return BotState(chat_id=TELEGRAM_CHAT_ID or None)


def save_state(state: BotState) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as e:
        print(f"save_state error: {e}", flush=True)


STATE_LOCK = threading.Lock()


# =========================================================
# STRATEGY HELPERS
# =========================================================
def candle_body_high(row: pd.Series) -> float:
    return float(max(row["open"], row["close"]))


def candle_body_low(row: pd.Series) -> float:
    return float(min(row["open"], row["close"]))


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = (
        df.set_index("timestamp")[["open", "high", "low", "close"]]
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
        .reset_index()
    )
    return out


def build_confirmed_pivots(m15: pd.DataFrame, swing_window: int = 2) -> pd.DataFrame:
    df = m15.copy()
    n = swing_window

    df["pivot_high"] = df["high"].where(
        df["high"] == df["high"].rolling(2 * n + 1, center=True).max()
    )
    df["pivot_low"] = df["low"].where(
        df["low"] == df["low"].rolling(2 * n + 1, center=True).min()
    )

    tf_delta = pd.Timedelta(minutes=15)
    df["pivot_high_confirmed_at"] = pd.NaT
    df["pivot_low_confirmed_at"] = pd.NaT

    hi_mask = df["pivot_high"].notna()
    lo_mask = df["pivot_low"].notna()

    df.loc[hi_mask, "pivot_high_confirmed_at"] = df.loc[hi_mask, "timestamp"] + n * tf_delta
    df.loc[lo_mask, "pivot_low_confirmed_at"] = df.loc[lo_mask, "timestamp"] + n * tf_delta

    return df


def get_ob_zone(ob_row: pd.Series, bullish: bool, use_body: bool = True) -> Tuple[float, float, float]:
    if use_body:
        zone_low = candle_body_low(ob_row)
        zone_high = candle_body_high(ob_row)
    else:
        zone_low = float(ob_row["low"])
        zone_high = float(ob_row["high"])

    ob_extreme = float(ob_row["low"]) if bullish else float(ob_row["high"])
    return zone_low, zone_high, ob_extreme


def to_ist_str(ts: pd.Timestamp) -> str:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(OUTPUT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def iso_utc(ts: pd.Timestamp) -> str:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def parse_ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s).tz_convert("UTC")


def format_price(x: float) -> str:
    return f"{x:.2f}"


def format_signal_message(sig: PendingSignal) -> str:
    side = "BUY" if sig.direction == "long" else "SELL"
    return (
        f"📡 XAUUSD SMC signal detected\n"
        f"Type: {side}\n"
        f"Signal time: {to_ist_str(parse_ts(sig.setup_time))}\n"
        f"Entry: {format_price(sig.entry)}\n"
        f"SL: {format_price(sig.sl)}\n"
        f"TP: {format_price(sig.tp)}\n"
        f"RR: 1:{RR:.0f}\n"
        f"SL Distance: {sig.sl_distance:.2f}\n"
        f"OB Time: {to_ist_str(parse_ts(sig.ob_time))}\n"
        f"Valid until: {to_ist_str(parse_ts(sig.expires_at))}"
    )


def format_entry_message(sig: PendingSignal) -> str:
    side = "BUY" if sig.direction == "long" else "SELL"
    return (
        f"✅ XAUUSD entry taken\n"
        f"Type: {side}\n"
        f"Entry time: {to_ist_str(parse_ts(sig.entry_time))}\n"
        f"Entry: {format_price(sig.entry)}\n"
        f"SL: {format_price(sig.sl)}\n"
        f"TP: {format_price(sig.tp)}\n"
        f"RR: 1:{RR:.0f}"
    )


def format_result_message(sig: PendingSignal, result: str, exit_time: pd.Timestamp, pnl_points: float) -> str:
    emoji = "🟢" if result == "win" else "🔴"
    side = "BUY" if sig.direction == "long" else "SELL"
    return (
        f"{emoji} XAUUSD trade result\n"
        f"Type: {side}\n"
        f"Entry time: {to_ist_str(parse_ts(sig.entry_time))}\n"
        f"Exit time: {to_ist_str(exit_time)}\n"
        f"Entry: {format_price(sig.entry)}\n"
        f"SL: {format_price(sig.sl)}\n"
        f"TP: {format_price(sig.tp)}\n"
        f"Result: {result.upper()}\n"
        f"PnL Points: {pnl_points:.2f}"
    )


def format_expired_message(sig: PendingSignal) -> str:
    side = "BUY" if sig.direction == "long" else "SELL"
    return (
        f"⌛ XAUUSD signal expired\n"
        f"Type: {side}\n"
        f"Signal time: {to_ist_str(parse_ts(sig.setup_time))}\n"
        f"Entry was not triggered within the waiting window."
    )


# =========================================================
# SMC DETECTION
# =========================================================
def detect_latest_smc_signal(m15: pd.DataFrame, used_bos_keys: set) -> Optional[PendingSignal]:
    """
    Detects only the latest valid, not-yet-used SMC setup.
    Setup logic:
      - M15 close breaks last confirmed pivot high/low
      - find last opposite candle within previous N M15 candles
      - entry at OB midpoint
      - SL beyond OB extreme +/- buffer
      - enforce min stop and SL > 0.49 filter
    """
    if m15.empty:
        return None

    m15 = build_confirmed_pivots(m15, SMC_SWING_WINDOW).reset_index(drop=True)

    start_i = max(10, SMC_SWING_WINDOW * 2 + 2)
    latest_signal: Optional[PendingSignal] = None

    for i in range(start_i, len(m15)):
        row = m15.iloc[i]
        now = row["timestamp"]

        hist = m15.iloc[:i].copy()

        piv_hi = hist[
            hist["pivot_high"].notna() &
            (hist["pivot_high_confirmed_at"] <= now)
        ]
        piv_lo = hist[
            hist["pivot_low"].notna() &
            (hist["pivot_low_confirmed_at"] <= now)
        ]

        last_hi = piv_hi.iloc[-1] if not piv_hi.empty else None
        last_lo = piv_lo.iloc[-1] if not piv_lo.empty else None

        direction = None
        bos_key = None

        if last_hi is not None and row["close"] > last_hi["pivot_high"]:
            bos_key = f"bull|{pd.Timestamp(last_hi['timestamp']).isoformat()}"
            if bos_key not in used_bos_keys:
                direction = "long"

        elif last_lo is not None and row["close"] < last_lo["pivot_low"]:
            bos_key = f"bear|{pd.Timestamp(last_lo['timestamp']).isoformat()}"
            if bos_key not in used_bos_keys:
                direction = "short"

        if direction is None or bos_key is None:
            continue

        prev_block = m15.iloc[max(0, i - SMC_OB_SEARCH_BACK):i].copy()
        if prev_block.empty:
            continue

        if direction == "long":
            ob_candidates = prev_block[prev_block["close"] < prev_block["open"]]
            if ob_candidates.empty:
                continue
            ob = ob_candidates.iloc[-1]
            zone_low, zone_high, ob_extreme = get_ob_zone(ob, bullish=True, use_body=SMC_OB_USE_BODY)
            entry = (zone_low + zone_high) / 2.0
            sl = ob_extreme - PRICE_BUFFER
            if entry - sl < MIN_STOP_DISTANCE:
                sl = entry - MIN_STOP_DISTANCE
            risk = entry - sl
            tp = entry + RR * risk

        else:
            ob_candidates = prev_block[prev_block["close"] > prev_block["open"]]
            if ob_candidates.empty:
                continue
            ob = ob_candidates.iloc[-1]
            zone_low, zone_high, ob_extreme = get_ob_zone(ob, bullish=False, use_body=SMC_OB_USE_BODY)
            entry = (zone_low + zone_high) / 2.0
            sl = ob_extreme + PRICE_BUFFER
            if sl - entry < MIN_STOP_DISTANCE:
                sl = entry + MIN_STOP_DISTANCE
            risk = sl - entry
            tp = entry - RR * risk

        if risk <= MIN_SL_FILTER:
            continue

        expires_at = row["timestamp"] + pd.Timedelta(minutes=ENTRY_WAIT_BARS_SMC)

        latest_signal = PendingSignal(
            bos_key=bos_key,
            direction=direction,
            setup_time=iso_utc(row["timestamp"]),
            entry=float(entry),
            sl=float(sl),
            tp=float(tp),
            sl_distance=float(risk),
            ob_time=iso_utc(ob["timestamp"]),
            ob_zone_low=float(zone_low),
            ob_zone_high=float(zone_high),
            expires_at=iso_utc(expires_at),
        )

    return latest_signal


# =========================================================
# ENTRY / RESULT MONITORING
# =========================================================
def find_entry_touch(m1: pd.DataFrame, sig: PendingSignal) -> Optional[pd.Timestamp]:
    setup_time = parse_ts(sig.setup_time)
    expires_at = parse_ts(sig.expires_at)

    window = m1[(m1["timestamp"] > setup_time) & (m1["timestamp"] <= expires_at)].copy()
    if window.empty:
        return None

    for _, r in window.iterrows():
        if float(r["low"]) <= sig.entry <= float(r["high"]):
            return pd.Timestamp(r["timestamp"]).tz_convert("UTC")

    return None


def resolve_trade_after_entry(m1: pd.DataFrame, sig: PendingSignal) -> Optional[Tuple[pd.Timestamp, str, float]]:
    """
    Returns (exit_time, result, pnl_points) if trade has already finished,
    else returns None.
    """
    if not sig.entry_taken or not sig.entry_time:
        return None

    entry_time = parse_ts(sig.entry_time)
    end_time = entry_time + pd.Timedelta(minutes=MAX_HOLD_BARS)

    window = m1[m1["timestamp"] >= entry_time].copy()
    if window.empty:
        return None

    # Restrict to available data and hold window
    window = window[window["timestamp"] <= end_time].copy()
    if window.empty:
        return None

    for _, row in window.iterrows():
        hi = float(row["high"])
        lo = float(row["low"])
        ts = pd.Timestamp(row["timestamp"]).tz_convert("UTC")

        if sig.direction == "long":
            hit_sl = lo <= sig.sl
            hit_tp = hi >= sig.tp

            if hit_sl and hit_tp:
                if CONSERVATIVE_SAME_CANDLE:
                    return ts, "loss", sig.entry - sig.sl
                return ts, "win", sig.tp - sig.entry
            elif hit_sl:
                return ts, "loss", sig.entry - sig.sl
            elif hit_tp:
                return ts, "win", sig.tp - sig.entry

        else:
            hit_sl = hi >= sig.sl
            hit_tp = lo <= sig.tp

            if hit_sl and hit_tp:
                if CONSERVATIVE_SAME_CANDLE:
                    return ts, "loss", sig.sl - sig.entry
                return ts, "win", sig.entry - sig.tp
            elif hit_sl:
                return ts, "loss", sig.sl - sig.entry
            elif hit_tp:
                return ts, "win", sig.entry - sig.tp

    # If max-hold window has fully passed and still no TP/SL, close by last available close
    latest_ts = pd.Timestamp(window.iloc[-1]["timestamp"]).tz_convert("UTC")
    if latest_ts >= end_time:
        last_close = float(window.iloc[-1]["close"])
        if sig.direction == "long":
            pnl = last_close - sig.entry
        else:
            pnl = sig.entry - last_close
        result = "win" if pnl > 0 else "loss"
        return latest_ts, result, pnl

    return None


# =========================================================
# BOT CORE
# =========================================================
def process_commands(state: BotState) -> BotState:
    try:
        updates = get_updates(state.telegram_offset, timeout_sec=25)
    except Exception as e:
        print(f"getUpdates error: {e}", flush=True)
        return state

    for upd in updates:
        state.telegram_offset = int(upd["update_id"]) + 1

        msg = upd.get("message", {})
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None
        text = msg.get("text", "") or ""
        cmd = normalize_command(text)

        if not cmd:
            continue

        # Learn chat_id automatically if not fixed
        if chat_id and not state.chat_id:
            state.chat_id = chat_id

        if cmd == "/startbot":
            state.bot_enabled = True
            if chat_id:
                state.chat_id = chat_id
            save_state(state)
            send_telegram_message(
                "✅ XAUUSD SMC bot is online.\n"
                "Logic: M15 BOS → last opposite candle OB → midpoint entry → SL beyond OB → TP 1:2\n"
                "Status: monitoring live data.",
                state.chat_id,
            )

        elif cmd == "/stopbot":
            state.bot_enabled = False
            save_state(state)
            send_telegram_message("🛑 XAUUSD SMC bot stopped.", state.chat_id)

        elif cmd == "/statusbot":
            pending = "None"
            if state.pending_signal:
                pending = state.pending_signal.get("status", "unknown")
            active = "Yes" if state.active_trade else "No"

            send_telegram_message(
                f"📊 XAUUSD SMC bot status\n"
                f"Enabled: {state.bot_enabled}\n"
                f"Symbol: {SYMBOL}\n"
                f"RR: 1:{RR:.0f}\n"
                f"Pending signal: {pending}\n"
                f"Active trade: {active}\n"
                f"Chat ID saved: {bool(state.chat_id)}",
                state.chat_id,
            )

        elif cmd == "/resetbot":
            state.pending_signal = None
            state.active_trade = None
            state.used_bos_keys = []
            save_state(state)
            send_telegram_message("♻️ XAUUSD SMC bot state reset.", state.chat_id)

        elif cmd == "/help":
            send_telegram_message(
                "Commands:\n"
                "/startbot\n"
                "/stopbot\n"
                "/statusbot\n"
                "/resetbot\n"
                "/help",
                state.chat_id,
            )

    return state


def scan_and_trade(state: BotState) -> BotState:
    if not state.bot_enabled:
        return state

    # Enforce one trade at a time
    if ONE_TRADE_AT_A_TIME and state.active_trade is not None:
        sig = PendingSignal(**state.active_trade)
        m1 = td_time_series(SYMBOL, "1min", TD_OUTPUTSIZE_1M)
        result = resolve_trade_after_entry(m1, sig)
        if result is not None and not sig.result_sent:
            exit_time, outcome, pnl_points = result
            send_telegram_message(format_result_message(sig, outcome, exit_time, pnl_points), state.chat_id)
            sig.result_sent = True
            sig.status = "closed"
            state.active_trade = None
            save_state(state)
        return state

    # If pending signal exists, monitor it first
    if state.pending_signal is not None:
        sig = PendingSignal(**state.pending_signal)
        now_utc = pd.Timestamp.utcnow().tz_localize("UTC") if pd.Timestamp.utcnow().tzinfo is None else pd.Timestamp.utcnow().tz_convert("UTC")

        # Send signal once
        if not sig.signal_sent:
            send_telegram_message(format_signal_message(sig), state.chat_id)
            sig.signal_sent = True
            state.pending_signal = asdict(sig)
            save_state(state)

        # Check expiry before entry
        if now_utc > parse_ts(sig.expires_at) and not sig.entry_taken:
            send_telegram_message(format_expired_message(sig), state.chat_id)
            sig.status = "expired"
            state.pending_signal = None
            # Mark BOS as used so it won't spam repeatedly
            if sig.bos_key not in state.used_bos_keys:
                state.used_bos_keys.append(sig.bos_key)
            save_state(state)
            return state

        # Check if entry was taken
        m1 = td_time_series(SYMBOL, "1min", TD_OUTPUTSIZE_1M)
        if not sig.entry_taken:
            entry_time = find_entry_touch(m1, sig)
            if entry_time is not None:
                sig.entry_taken = True
                sig.entry_time = iso_utc(entry_time)
                sig.status = "active"
                send_telegram_message(format_entry_message(sig), state.chat_id)

                # Move to active trade
                state.active_trade = asdict(sig)
                state.pending_signal = None
                if sig.bos_key not in state.used_bos_keys:
                    state.used_bos_keys.append(sig.bos_key)
                save_state(state)
                return state

        state.pending_signal = asdict(sig)
        save_state(state)
        return state

    # No pending signal and no active trade: scan for new setup
    m1 = td_time_series(SYMBOL, "1min", TD_OUTPUTSIZE_1M)
    m15 = resample_ohlc(m1, SMC_ENTRY_TF)

    used = set(state.used_bos_keys or [])
    sig = detect_latest_smc_signal(m15, used)

    if sig is not None:
        # Avoid stale setups that are already expired before we even send them
        now_utc = pd.Timestamp.utcnow().tz_localize("UTC") if pd.Timestamp.utcnow().tzinfo is None else pd.Timestamp.utcnow().tz_convert("UTC")
        if parse_ts(sig.expires_at) > now_utc:
            state.pending_signal = asdict(sig)
            save_state(state)

    return state


def bot_loop():
    print("Starting bot loop...", flush=True)
    telegram_delete_webhook()

    state = load_state()
    save_state(state)

    while True:
        try:
            with STATE_LOCK:
                state = load_state()
                state = process_commands(state)
                state = scan_and_trade(state)
                save_state(state)

        except Exception as e:
            print(f"Main loop error: {e}", flush=True)

        time.sleep(POLL_SECONDS)


# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    thread = threading.Thread(target=bot_loop, daemon=True)
    thread.start()

    app.run(host="0.0.0.0", port=PORT)
