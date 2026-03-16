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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
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

# =========================================
# SIMPLE STATE
# =========================================
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "bot_enabled": False,
    "rr": DEFAULT_RR,
    "last_signal_id": None,
    "pending_signal": None,
    "telegram_offset": 0,
    "last_checked_ts": 0.0,
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
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def round5(x: float) -> float:
    return round(float(x), 5)


def parse_twelvedata_datetime_utc(dt_str: str) -> float:
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def format_epoch_local(epoch_sec: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_sec)) + f" {OUTPUT_TIMEZONE}"


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
    """
    Important fix for 409 Conflict:
    If a webhook exists, getUpdates polling will conflict.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    resp = requests.post(url, json={"drop_pending_updates": False}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    print("[bot] deleteWebhook response:", data)

    # Optional visibility
    info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    info_resp = requests.get(info_url, timeout=20)
    info_resp.raise_for_status()
    print("[bot] getWebhookInfo:", info_resp.json())


def send_telegram_message(text: str) -> None:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    print(f"[bot] sending message to chat_id={TELEGRAM_CHAT_ID}: {text[:120]!r}")
    try:
        tg_api("sendMessage", payload, timeout=20)
        print("[bot] message sent successfully")
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
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error: {data}")

    values = data.get("values", [])
    if not values:
        raise RuntimeError("No candle data returned from Twelve Data.")

    rows: List[Candle] = []
    for v in values:
        rows.append(
            Candle(
                timestamp=v["datetime"],
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            )
        )

    rows.sort(key=lambda r: r.timestamp)
    return rows


# =========================================
# STRATEGY CORE
# =========================================
def build_pivots(rows: List[Candle], n: int):
    pivot_high = [None] * len(rows)
    pivot_low = [None] * len(rows)

    for i in range(n, len(rows) - n):
        is_high = True
        is_low = True
        for j in range(i - n, i + n + 1):
            if rows[j].high > rows[i].high:
                is_high = False
            if rows[j].low < rows[i].low:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            pivot_high[i] = rows[i].high
        if is_low:
            pivot_low[i] = rows[i].low

    return pivot_high, pivot_low


def find_last_pivot_index(pivot_array: List[Optional[float]], max_idx: int) -> int:
    for i in range(max_idx, -1, -1):
        if pivot_array[i] is not None:
            return i
    return -1


def detect_smc_signal(rows: List[Candle], rr: float) -> Optional[Signal]:
    n = SWING_WINDOW
    i = len(rows) - 2  # latest closed candle

    if i < max(10, 2 * n + 2):
        return None

    pivot_high, pivot_low = build_pivots(rows, n)
    latest = rows[i]

    last_high_idx = find_last_pivot_index(pivot_high, i - 1)
    last_low_idx = find_last_pivot_index(pivot_low, i - 1)

    direction = None
    if last_high_idx != -1 and latest.close > float(pivot_high[last_high_idx]):
        direction = "long"
    elif last_low_idx != -1 and latest.close < float(pivot_low[last_low_idx]):
        direction = "short"
    else:
        return None

    start = max(0, i - OB_LOOKBACK)
    ob_idx = -1

    if direction == "long":
        for k in range(i - 1, start - 1, -1):
            if rows[k].close < rows[k].open:
                ob_idx = k
                break

        if ob_idx == -1:
            return None

        zone_low = min(rows[ob_idx].open, rows[ob_idx].close)
        zone_high = max(rows[ob_idx].open, rows[ob_idx].close)
        entry = round5((zone_low + zone_high) / 2.0)
        sl = round5(rows[ob_idx].low - SL_BUFFER)
        risk = round5(entry - sl)

        if not (risk > MIN_SL_DISTANCE):
            return None

        tp = round5(entry + rr * risk)
        bos_epoch = parse_twelvedata_datetime_utc(latest.timestamp)

        return Signal(
            signal_id=f"long_{latest.timestamp}",
            direction="long",
            bos_time=latest.timestamp,
            ob_time=rows[ob_idx].timestamp,
            zone_low=round5(zone_low),
            zone_high=round5(zone_high),
            entry=entry,
            sl=sl,
            tp=tp,
            risk=risk,
            expires_at_epoch=bos_epoch + ENTRY_WAIT_BARS * 60,
            sent_entry_alert=False,
            is_active_trade=False,
            entry_time=None,
        )

    for k in range(i - 1, start - 1, -1):
        if rows[k].close > rows[k].open:
            ob_idx = k
            break

    if ob_idx == -1:
        return None

    zone_low = min(rows[ob_idx].open, rows[ob_idx].close)
    zone_high = max(rows[ob_idx].open, rows[ob_idx].close)
    entry = round5((zone_low + zone_high) / 2.0)
    sl = round5(rows[ob_idx].high + SL_BUFFER)
    risk = round5(sl - entry)

    if not (risk > MIN_SL_DISTANCE):
        return None

    tp = round5(entry - rr * risk)
    bos_epoch = parse_twelvedata_datetime_utc(latest.timestamp)

    return Signal(
        signal_id=f"short_{latest.timestamp}",
        direction="short",
        bos_time=latest.timestamp,
        ob_time=rows[ob_idx].timestamp,
        zone_low=round5(zone_low),
        zone_high=round5(zone_high),
        entry=entry,
        sl=sl,
        tp=tp,
        risk=risk,
        expires_at_epoch=bos_epoch + ENTRY_WAIT_BARS * 60,
        sent_entry_alert=False,
        is_active_trade=False,
        entry_time=None,
    )


def manage_pending_trade(rows: List[Candle]) -> None:
    with state_lock:
        pending_raw = state["pending_signal"]
        rr = state["rr"]

    if not pending_raw:
        return

    sig = Signal(**pending_raw)

    # -------------------------
    # WAIT FOR ENTRY
    # -------------------------
    if not sig.is_active_trade:
        bos_epoch = parse_twelvedata_datetime_utc(sig.bos_time)

        for r in rows:
            r_epoch = parse_twelvedata_datetime_utc(r.timestamp)
            if r_epoch <= bos_epoch:
                continue
            if r_epoch > sig.expires_at_epoch:
                break

            if r.low <= sig.entry <= r.high:
                sig.is_active_trade = True
                sig.entry_time = r.timestamp

                if not sig.sent_entry_alert:
                    send_telegram_message(
                        "📍 *ENTRY ALERT — XAUUSD SMC*\n"
                        f"Direction: *{sig.direction.upper()}*\n"
                        f"Entry time: `{sig.entry_time}`\n"
                        f"Entry: `{sig.entry}`\n"
                        f"SL: `{sig.sl}`\n"
                        f"TP: `{sig.tp}`\n"
                        f"RR: `1:{rr}`"
                    )
                    sig.sent_entry_alert = True

                with state_lock:
                    state["pending_signal"] = asdict(sig)
                print(f"[bot] Trade activated: {sig.signal_id}")
                break

        if not sig.is_active_trade:
            last_closed_epoch = parse_twelvedata_datetime_utc(rows[-2].timestamp)
            if last_closed_epoch > sig.expires_at_epoch:
                send_telegram_message(
                    "⌛ *Signal expired*\n"
                    f"Direction: *{sig.direction.upper()}*\n"
                    "Entry was not triggered before expiry."
                )
                print(f"[bot] Signal expired: {sig.signal_id}")
                with state_lock:
                    state["pending_signal"] = None
            return

    # -------------------------
    # MANAGE ACTIVE TRADE
    # -------------------------
    bars_from_entry = 0

    for r in rows:
        if not sig.entry_time:
            continue
        if parse_twelvedata_datetime_utc(r.timestamp) < parse_twelvedata_datetime_utc(sig.entry_time):
            continue

        bars_from_entry += 1

        if sig.direction == "long":
            hit_sl = r.low <= sig.sl
            hit_tp = r.high >= sig.tp

            # conservative same-candle conflict = SL
            if hit_sl or hit_tp:
                outcome = "loss" if hit_sl else "win"
                pnl = round5((sig.entry - sig.sl) if hit_sl else (sig.tp - sig.entry))
                send_telegram_message(
                    ("❌" if outcome == "loss" else "✅") + " *RESULT — XAUUSD SMC*\n"
                    "Direction: *LONG*\n"
                    f"Entry time: `{sig.entry_time}`\n"
                    f"Exit time: `{r.timestamp}`\n"
                    f"Entry: `{sig.entry}`\n"
                    f"SL: `{sig.sl}`\n"
                    f"TP: `{sig.tp}`\n"
                    f"Outcome: *{outcome.upper()}*\n"
                    f"PnL points: `{pnl}`"
                )
                print(f"[bot] Trade closed: {sig.signal_id}, outcome={outcome}")
                with state_lock:
                    state["pending_signal"] = None
                return

        else:
            hit_sl = r.high >= sig.sl
            hit_tp = r.low <= sig.tp

            # conservative same-candle conflict = SL
            if hit_sl or hit_tp:
                outcome = "loss" if hit_sl else "win"
                pnl = round5((sig.sl - sig.entry) if hit_sl else (sig.entry - sig.tp))
                send_telegram_message(
                    ("❌" if outcome == "loss" else "✅") + " *RESULT — XAUUSD SMC*\n"
                    "Direction: *SHORT*\n"
                    f"Entry time: `{sig.entry_time}`\n"
                    f"Exit time: `{r.timestamp}`\n"
                    f"Entry: `{sig.entry}`\n"
                    f"SL: `{sig.sl}`\n"
                    f"TP: `{sig.tp}`\n"
                    f"Outcome: *{outcome.upper()}*\n"
                    f"PnL points: `{pnl}`"
                )
                print(f"[bot] Trade closed: {sig.signal_id}, outcome={outcome}")
                with state_lock:
                    state["pending_signal"] = None
                return

        if bars_from_entry >= MAX_TRADE_BARS:
            send_telegram_message(
                "⌛ *Trade timeout — XAUUSD SMC*\n"
                f"Direction: *{sig.direction.upper()}*\n"
                "Trade closed due to max holding bars without TP/SL hit."
            )
            print(f"[bot] Trade timeout: {sig.signal_id}")
            with state_lock:
                state["pending_signal"] = None
            return


# =========================================
# BOT COMMANDS
# =========================================
def get_status_text() -> str:
    with state_lock:
        enabled = state["bot_enabled"]
        rr = state["rr"]
        pending = state["pending_signal"]
        last_signal_id = state["last_signal_id"]

    active_trade = False
    pending_signal = False
    if pending:
        pending_signal = True
        active_trade = bool(pending.get("is_active_trade"))

    return (
        "*SMC bot status*\n"
        f"Enabled: `{enabled}`\n"
        f"RR: `1:{rr}`\n"
        f"Min SL distance: `>{MIN_SL_DISTANCE}`\n"
        f"Pending signal: `{pending_signal}`\n"
        f"Active trade: `{active_trade}`\n"
        f"Last signal ID: `{last_signal_id or 'none'}`"
    )


def handle_command(text: str) -> None:
    parts = text.strip().split()
    command = parts[0].lower().split("@")[0]

    print(f"[bot] command received: {command}, full text: {text!r}")

    if command == "/startbt":
        with state_lock:
            state["bot_enabled"] = True
            rr = state["rr"]
        send_telegram_message(
            "✅ *SMC bot started*\n"
            f"Monitoring *{SYMBOL}* on `{INTERVAL}` candles.\n"
            f"RR: `1:{rr}`\n"
            f"Min SL distance: `>{MIN_SL_DISTANCE}`"
        )
        return

    if command == "/stopbt":
        with state_lock:
            state["bot_enabled"] = False
        send_telegram_message("🛑 *SMC bot stopped*")
        return

    if command == "/statusbt":
        send_telegram_message(get_status_text())
        return

    if command == "/setrr":
        if len(parts) < 2:
            send_telegram_message("Usage: `/setrr 3`")
            return
        try:
            rr = float(parts[1])
            if rr <= 0:
                raise ValueError
        except ValueError:
            send_telegram_message("❌ Invalid RR value.")
            return

        with state_lock:
            state["rr"] = rr
        send_telegram_message(f"✅ RR updated to `1:{rr}`")
        return

    if command == "/resetbot":
        with state_lock:
            state["bot_enabled"] = False
            state["rr"] = DEFAULT_RR
            state["last_signal_id"] = None
            state["pending_signal"] = None
        send_telegram_message("♻️ Bot state cleared.")
        return

    if command == "/testmsg":
        send_telegram_message("✅ Bot command system is working.")
        return

    send_telegram_message(
        "Unknown command.\n\n"
        "Available commands:\n"
        "`/startbt`\n"
        "`/stopbt`\n"
        "`/statusbt`\n"
        "`/setrr 3`\n"
        "`/testmsg`\n"
        "`/resetbot`"
    )


# =========================================
# BACKGROUND LOOPS
# =========================================
def telegram_poll_loop() -> None:
    print("[bot] Telegram polling loop started")
    while True:
        try:
            with state_lock:
                offset = int(state["telegram_offset"])

            updates = get_updates(offset)
            if updates:
                print(f"[bot] updates count = {len(updates)}")

            for upd in updates:
                print("[bot] raw update:", json.dumps(upd))

                new_offset = int(upd["update_id"]) + 1
                with state_lock:
                    state["telegram_offset"] = new_offset

                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(msg.get("text", "")).strip()

                print(f"[bot] incoming chat_id={chat_id}, expected_chat_id={TELEGRAM_CHAT_ID}, text={text!r}")

                if chat_id != TELEGRAM_CHAT_ID:
                    print(f"[bot] ignored message from different chat_id={chat_id}")
                    continue

                if not text.startswith("/"):
                    print("[bot] ignored non-command message")
                    continue

                handle_command(text)

        except Exception as e:
            print(f"[bot] Telegram polling error: {e}")

        time.sleep(POLL_SLEEP_SECONDS)


def signal_loop() -> None:
    print("[bot] Signal loop started")
    while True:
        try:
            with state_lock:
                enabled = state["bot_enabled"]
                rr = float(state["rr"])

            if enabled:
                rows = get_twelvedata_candles(SYMBOL, INTERVAL, LOOKBACK_BARS)
                print(f"[bot] fetched {len(rows)} candles")

                if len(rows) >= 30:
                    manage_pending_trade(rows)

                    with state_lock:
                        has_pending = state["pending_signal"] is not None

                    if not has_pending:
                        signal = detect_smc_signal(rows, rr)
                        if signal:
                            with state_lock:
                                last_signal_id = state["last_signal_id"]

                            if signal.signal_id != last_signal_id:
                                with state_lock:
                                    state["last_signal_id"] = signal.signal_id
                                    state["pending_signal"] = asdict(signal)

                                print(f"[bot] New signal detected: {signal.signal_id}")
                                send_telegram_message(
                                    "🚨 *LIVE SIGNAL DETECTED — XAUUSD SMC*\n"
                                    f"Direction: *{signal.direction.upper()}*\n"
                                    f"BOS time: `{signal.bos_time}`\n"
                                    f"OB time: `{signal.ob_time}`\n"
                                    f"Zone: `{signal.zone_low} - {signal.zone_high}`\n"
                                    f"Entry: `{signal.entry}`\n"
                                    f"SL: `{signal.sl}`\n"
                                    f"TP: `{signal.tp}`\n"
                                    f"RR: `1:{rr}`\n"
                                    f"SL distance: `{signal.risk}`"
                                )
                            else:
                                print(f"[bot] Duplicate signal ignored: {signal.signal_id}")
                        else:
                            print("[bot] No new signal on this cycle")

        except Exception as e:
            print(f"[bot] Signal loop error: {e}")

        time.sleep(SIGNAL_CHECK_SECONDS)


def start_background_threads() -> None:
    t1 = threading.Thread(target=telegram_poll_loop, daemon=True)
    t2 = threading.Thread(target=signal_loop, daemon=True)
    t1.start()
    t2.start()
    print("[bot] Background threads started")


# =========================================
# MAIN
# =========================================
require_env()
clear_telegram_webhook()
start_background_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    print(f"[bot] Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)