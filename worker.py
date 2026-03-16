import os
import time
import json
import threading
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

import requests

# =========================================
# CONFIG FROM ENV
# =========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # optional
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
}

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
    resp = requests.post(url, json={"drop_pending_updates": False}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    print("[worker] deleteWebhook response:", data)

    info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    info_resp = requests.get(info_url, timeout=20)
    info_resp.raise_for_status()
    print("[worker] getWebhookInfo:", info_resp.json())


def send_telegram_message(text: str) -> None:
    chat_id = get_effective_chat_id()
    if not chat_id:
        print("[worker] send skipped: no chat_id available yet")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        print(f"[worker] sending message to {chat_id}: {text[:100]!r}")
        tg_api("sendMessage", payload, timeout=20)
        print("[worker] message sent successfully")
    except Exception as e:
        print(f"[worker] Telegram send error: {e}")
        set_last_error(f"Telegram send error: {e}")


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
    i = len(rows) - 2

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

                print(f"[worker] Trade activated: {sig.signal_id}")
                break

        if not sig.is_active_trade:
            last_closed_epoch = parse_twelvedata_datetime_utc(rows[-2].timestamp)
            if last_closed_epoch > sig.expires_at_epoch:
                send_telegram_message(
                    "⌛ *Signal expired*\n"
                    f"Direction: *{sig.direction.upper()}*\n"
                    "Entry was not triggered before expiry."
                )
                with state_lock:
                    state["pending_signal"] = None
                print(f"[worker] Signal expired: {sig.signal_id}")
            return

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
                with state_lock:
                    state["pending_signal"] = None
                print(f"[worker] Trade closed: {sig.signal_id}, outcome={outcome}")
                return
        else:
            hit_sl = r.high >= sig.sl
            hit_tp = r.low <= sig.tp
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
                with state_lock:
                    state["pending_signal"] = None
                print(f"[worker] Trade closed: {sig.signal_id}, outcome={outcome}")
                return

        if bars_from_entry >= MAX_TRADE_BARS:
            send_telegram_message(
                "⌛ *Trade timeout — XAUUSD SMC*\n"
                f"Direction: *{sig.direction.upper()}*\n"
                "Trade closed due to max holding bars without TP/SL hit."
            )
            with state_lock:
                state["pending_signal"] = None
            print(f"[worker] Trade timeout: {sig.signal_id}")
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
        detected_chat_id = state["detected_chat_id"]
        last_fetch = state["last_candle_fetch_count"]
        last_closed = state["last_closed_candle"]
        last_error = state["last_error"]

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
        f"Detected Chat ID: `{detected_chat_id or 'not set yet'}`\n"
        f"Last signal ID: `{last_signal_id or 'none'}`\n"
        f"Last candle fetch count: `{last_fetch}`\n"
        f"Last closed candle: `{last_closed or 'none'}`\n"
        f"Last error: `{last_error or 'none'}`"
    )


def handle_command(text: str) -> None:
    parts = text.strip().split()
    command = parts[0].lower().split("@")[0]

    print(f"[worker] command received: {command} | text={text!r}")

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
            state["bot_enabled"] = AUTO_START_BOT
            state["rr"] = DEFAULT_RR
            state["last_signal_id"] = None
            state["pending_signal"] = None
            state["api_credit_notified"] = False
            state["api_resumed_notified"] = False
            state["last_error"] = None
        send_telegram_message("♻️ Bot state cleared.")
        return

    if command == "/testmsg":
        send_telegram_message("✅ Bot command system is working.")
        return

    if command == "/whoami":
        send_telegram_message(get_status_text())
        return

    send_telegram_message(
        "Unknown command.\n\n"
        "Available commands:\n"
        "`/startbt`\n"
        "`/stopbt`\n"
        "`/statusbt`\n"
        "`/setrr 3`\n"
        "`/testmsg`\n"
        "`/resetbot`\n"
        "`/whoami`"
    )


# =========================================
# BACKGROUND LOOPS
# =========================================
def telegram_poll_loop() -> None:
    print("[worker] Telegram polling loop started")
    while True:
        try:
            with state_lock:
                offset = int(state["telegram_offset"])

            print(f"[worker] polling Telegram with offset={offset}")
            updates = get_updates(offset)

            if updates:
                print(f"[worker] updates count = {len(updates)}")

            for upd in updates:
                print("[worker] raw update:", json.dumps(upd))

                new_offset = int(upd["update_id"]) + 1
                with state_lock:
                    state["telegram_offset"] = new_offset

                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(msg.get("text", "")).strip()

                print(f"[worker] incoming chat_id={chat_id}, current_chat_id={get_effective_chat_id()}, text={text!r}")

                if AUTO_CAPTURE_CHAT_ID and not get_effective_chat_id() and chat_id:
                    with state_lock:
                        state["detected_chat_id"] = chat_id
                    print(f"[worker] auto-detected TELEGRAM_CHAT_ID={chat_id}")

                    if SEND_RESTART_MESSAGE:
                        send_telegram_message(
                            "✅ *XAUUSD SMC bot connected*\n"
                            "Chat ID auto-detected successfully.\n"
                            "You can now use commands like `/startbt` and `/statusbt`."
                        )

                effective_chat_id = get_effective_chat_id()
                if effective_chat_id and chat_id != effective_chat_id:
                    print(f"[worker] ignored message from other chat_id={chat_id}")
                    continue

                if not text.startswith("/"):
                    print("[worker] ignored non-command message")
                    continue

                handle_command(text)

        except Exception as e:
            err = f"Telegram polling error: {e}"
            print(f"[worker] {err}")
            set_last_error(err)

            if "409" in str(e) or "Conflict" in str(e):
                try:
                    clear_telegram_webhook()
                except Exception as inner:
                    print(f"[worker] webhook clear retry failed: {inner}")

        time.sleep(POLL_SLEEP_SECONDS)


def signal_loop() -> None:
    print("[worker] Signal loop started")
    if AUTO_START_BOT:
        print("[worker] AUTO_START_BOT=true, signal scanning enabled at startup")

    while True:
        try:
            print("[worker] signal loop heartbeat")

            with state_lock:
                enabled = state["bot_enabled"]
                rr = float(state["rr"])

            if enabled:
                print("[worker] signal scan started")
                rows = get_twelvedata_candles(SYMBOL, INTERVAL, LOOKBACK_BARS)

                send_resume_notice = False
                with state_lock:
                    state["last_candle_fetch_count"] = len(rows)
                    state["last_signal_scan_time_utc"] = now_utc_str()
                    if len(rows) >= 2:
                        state["last_closed_candle"] = asdict(rows[-2])
                    state["last_error"] = None

                    if state["api_credit_notified"]:
                        state["api_credit_notified"] = False
                        state["api_resumed_notified"] = True
                        send_resume_notice = True
                    else:
                        state["api_resumed_notified"] = False

                if send_resume_notice:
                    send_telegram_message(
                        "✅ *XAUUSD BOT UPDATE*\n\n"
                        "Twelve Data API is working again.\n"
                        "Signal scanning has resumed."
                    )

                print(f"[worker] fetched {len(rows)} candles")
                if len(rows) >= 2:
                    r = rows[-2]
                    print(
                        f"[worker] latest closed candle: ts={r.timestamp}, "
                        f"o={r.open}, h={r.high}, l={r.low}, c={r.close}"
                    )

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

                                print(f"[worker] New signal detected: {signal.signal_id}")
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
                                print(f"[worker] Duplicate signal ignored: {signal.signal_id}")
                        else:
                            print("[worker] No new signal on this cycle")
            else:
                print("[worker] bot_enabled is false, signal scan skipped")

        except Exception as e:
            err = f"Signal loop error: {e}"
            print(f"[worker] {err}")
            set_last_error(err)

            if is_credit_error(e):
                should_notify = False
                with state_lock:
                    if not state["api_credit_notified"]:
                        state["api_credit_notified"] = True
                        state["api_resumed_notified"] = False
                        should_notify = True

                if should_notify:
                    send_telegram_message(
                        "⚠️ *XAUUSD BOT WARNING*\n\n"
                        "Twelve Data API credits have been exhausted for today.\n\n"
                        "Signal scanning is paused until credits reset.\n\n"
                        "The bot will automatically resume when the API works again."
                    )

        time.sleep(SIGNAL_CHECK_SECONDS)


def main() -> None:
    require_env()
    clear_telegram_webhook()

    t1 = threading.Thread(target=telegram_poll_loop, daemon=True)
    t2 = threading.Thread(target=signal_loop, daemon=True)
    t1.start()
    t2.start()

    print("[worker] Background threads started")
    print(f"[worker] AUTO_START_BOT={AUTO_START_BOT}")
    print(f"[worker] AUTO_CAPTURE_CHAT_ID={AUTO_CAPTURE_CHAT_ID}")
    print(f"[worker] TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID or 'not provided'}")

    if SEND_RESTART_MESSAGE and TELEGRAM_CHAT_ID:
        send_telegram_message("✅ XAUUSD SMC worker restarted and background loops started.")

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()