from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

import requests
import pandas as pd
from flask import Flask, jsonify


# =========================================================
# CONFIG
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

SYMBOL = os.getenv("SYMBOL", "XAU/USD").strip()
INTERVAL = os.getenv("INTERVAL", "1min").strip()
SOURCE_TIMEZONE = os.getenv("SOURCE_TIMEZONE", "UTC").strip()
OUTPUT_TIMEZONE = os.getenv("OUTPUT_TIMEZONE", "Asia/Kolkata").strip()

DEFAULT_RR = float(os.getenv("DEFAULT_RR", "2.0"))
MIN_STOP_DISTANCE = float(os.getenv("MIN_STOP_DISTANCE", "0.20"))
PRICE_BUFFER = float(os.getenv("PRICE_BUFFER", "0.05"))

LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "300"))
SMC_SWING_WINDOW = int(os.getenv("SMC_SWING_WINDOW", "2"))
SMC_OB_SEARCH_BACK = int(os.getenv("SMC_OB_SEARCH_BACK", "8"))
SMC_OB_USE_BODY = os.getenv("SMC_OB_USE_BODY", "true").lower() == "true"
ENTRY_WAIT_BARS = int(os.getenv("ENTRY_WAIT_BARS", "180"))
MAX_HOLD_BARS = int(os.getenv("MAX_HOLD_BARS", "180"))

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
CONSERVATIVE_SAME_CANDLE = os.getenv("CONSERVATIVE_SAME_CANDLE", "true").lower() == "true"

# IMPORTANT:
# Keep this TRUE if you want Telegram commands to work
ENABLE_COMMAND_POLLING = os.getenv("ENABLE_COMMAND_POLLING", "true").lower() == "true"

SAFE_TELEGRAM_SEND = os.getenv("SAFE_TELEGRAM_SEND", "true").lower() == "true"

PORT = int(os.getenv("PORT", "10000"))


# =========================================================
# FLASK APP FOR RENDER
# =========================================================
app = Flask(__name__)


@app.get("/")
def home():
    with state_lock:
        enabled = BOT_STATE["enabled"]
        signals = BOT_STATE.get("signals", [])
    return jsonify({
        "ok": True,
        "service": "xauusd-smc-bot",
        "enabled": enabled,
        "tracked_signals": len(signals),
    })


@app.get("/health")
def health():
    with state_lock:
        enabled = BOT_STATE["enabled"]
        signals = BOT_STATE.get("signals", [])
    return jsonify({
        "ok": True,
        "enabled": enabled,
        "tracked_signals": len(signals),
    })


# =========================================================
# RUNTIME STATE
# =========================================================
state_lock = threading.Lock()

BOT_STATE: Dict[str, Any] = {
    "enabled": False,
    "rr": DEFAULT_RR,
    "last_update_id": 0,
    "signals": [],          # list[dict]
    "seen_signal_ids": [],  # recent signal ids
}


# =========================================================
# DATA MODEL
# =========================================================
@dataclass
class ManagedSignal:
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
    expires_at: str
    status: str = "pending"      # pending / active / win / loss / expired / timeout
    sent_entry_alert: bool = False
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    pnl_points: Optional[float] = None
    bars_held: int = 0


# =========================================================
# HELPERS
# =========================================================
def require_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def round5(x: float) -> float:
    return round(float(x), 5)


def now_utc() -> pd.Timestamp:
    ts = pd.Timestamp.utcnow()
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def fmt_ts(ts) -> str:
    if ts is None or pd.isna(ts):
        return "NA"
    x = pd.Timestamp(ts)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    else:
        x = x.tz_convert("UTC")
    return x.tz_convert(OUTPUT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def esc_html(s: Any) -> str:
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def candle_body_high(row: pd.Series) -> float:
    return max(float(row["open"]), float(row["close"]))


def candle_body_low(row: pd.Series) -> float:
    return min(float(row["open"]), float(row["close"]))


def get_current_rr() -> float:
    with state_lock:
        rr = BOT_STATE.get("rr", DEFAULT_RR)
    return rr if rr > 0 else DEFAULT_RR


# =========================================================
# TELEGRAM
# =========================================================
def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        msg = f"Telegram send failed: {r.status_code} {r.text}"
        if SAFE_TELEGRAM_SEND:
            print(msg, flush=True)
            return
        raise RuntimeError(msg)


def get_updates(offset: Optional[int] = None, timeout: int = 20) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(url, params=params, timeout=timeout + 10)
    r.raise_for_status()
    return r.json()


def print_webhook_info() -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    print("Webhook info:", r.json(), flush=True)


def clear_webhook() -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    print("Webhook cleared:", r.json(), flush=True)


def send_startup_message_once() -> None:
    marker_file = "/tmp/xauusd_smc_started.marker"

    if os.path.exists(marker_file):
        print("Startup message already sent; skipping duplicate.", flush=True)
        return

    with open(marker_file, "w") as f:
        f.write(str(time.time()))

    send_telegram_message(
        "✅ <b>XAUUSD SMC bot is online.</b>\n"
        f"Command polling: <code>{esc_html(ENABLE_COMMAND_POLLING)}</code>\n"
        "Multiple signals and trades are tracked separately.\n"
        "Use <code>/startbt</code> to begin scanning."
    )


# =========================================================
# MARKET DATA
# =========================================================
def get_twelvedata_candles(symbol: str, interval: str, outputsize: int) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": SOURCE_TIMEZONE,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()

    if js.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {js}")

    values = js.get("values", [])
    if not values:
        raise RuntimeError("No candle data returned from TwelveData")

    rows = []
    for v in values:
        ts = pd.Timestamp(str(v["datetime"]).replace(" ", "T"))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        rows.append({
            "timestamp": ts,
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        })

    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return df


# =========================================================
# SMC LOGIC
# =========================================================
def build_confirmed_pivots(m1: pd.DataFrame, swing_window: int = 2) -> pd.DataFrame:
    df = m1.copy()
    n = swing_window

    highs = df["high"]
    lows = df["low"]

    df["pivot_high"] = highs.where(highs == highs.rolling(2 * n + 1, center=True).max())
    df["pivot_low"] = lows.where(lows == lows.rolling(2 * n + 1, center=True).min())

    tf_delta = pd.Timedelta(minutes=1)

    df["pivot_high_confirmed_at"] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    df["pivot_low_confirmed_at"] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")

    df["pivot_high_confirmed_at"] = (
        df["timestamp"] + n * tf_delta
    ).where(df["pivot_high"].notna(), pd.NaT)

    df["pivot_low_confirmed_at"] = (
        df["timestamp"] + n * tf_delta
    ).where(df["pivot_low"].notna(), pd.NaT)

    return df


def get_ob_zone(ob_row: pd.Series, bullish: bool, use_body: bool = True):
    if use_body:
        zone_low = candle_body_low(ob_row)
        zone_high = candle_body_high(ob_row)
    else:
        zone_low = float(ob_row["low"])
        zone_high = float(ob_row["high"])

    ob_extreme = float(ob_row["low"]) if bullish else float(ob_row["high"])
    return zone_low, zone_high, ob_extreme


def detect_smc_signal_latest_closed(m1: pd.DataFrame) -> Optional[ManagedSignal]:
    rr = get_current_rr()
    df = build_confirmed_pivots(m1, SMC_SWING_WINDOW).reset_index(drop=True)

    # Backtest-aligned logic: latest available closed candle
    i = len(df) - 1
    if i < max(10, SMC_SWING_WINDOW * 2 + 2):
        return None

    row = df.iloc[i]
    now = row["timestamp"]

    hist = df.iloc[:i].copy()

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

    if last_hi is not None and float(row["close"]) > float(last_hi["pivot_high"]):
        direction = "long"
    elif last_lo is not None and float(row["close"]) < float(last_lo["pivot_low"]):
        direction = "short"
    else:
        return None

    prev_block = df.iloc[max(0, i - SMC_OB_SEARCH_BACK):i].copy()
    if prev_block.empty:
        return None

    if direction == "long":
        ob_candidates = prev_block[prev_block["close"] < prev_block["open"]]
        if ob_candidates.empty:
            return None

        ob = ob_candidates.iloc[-1]
        zone_low, zone_high, ob_extreme = get_ob_zone(ob, bullish=True, use_body=SMC_OB_USE_BODY)

        entry = (zone_low + zone_high) / 2.0
        sl = ob_extreme - PRICE_BUFFER
        if entry - sl < MIN_STOP_DISTANCE:
            sl = entry - MIN_STOP_DISTANCE
        risk = entry - sl
        tp = entry + rr * risk

    else:
        ob_candidates = prev_block[prev_block["close"] > prev_block["open"]]
        if ob_candidates.empty:
            return None

        ob = ob_candidates.iloc[-1]
        zone_low, zone_high, ob_extreme = get_ob_zone(ob, bullish=False, use_body=SMC_OB_USE_BODY)

        entry = (zone_low + zone_high) / 2.0
        sl = ob_extreme + PRICE_BUFFER
        if sl - entry < MIN_STOP_DISTANCE:
            sl = entry + MIN_STOP_DISTANCE
        risk = sl - entry
        tp = entry - rr * risk

    bos_time = pd.Timestamp(row["timestamp"])
    expires_at = bos_time + pd.Timedelta(minutes=ENTRY_WAIT_BARS)
    signal_id = f"{direction}_{bos_time.isoformat()}_{round5(entry)}"

    return ManagedSignal(
        signal_id=signal_id,
        direction=direction,
        bos_time=bos_time.isoformat(),
        ob_time=pd.Timestamp(ob["timestamp"]).isoformat(),
        zone_low=round5(zone_low),
        zone_high=round5(zone_high),
        entry=round5(entry),
        sl=round5(sl),
        tp=round5(tp),
        risk=round5(risk),
        expires_at=expires_at.isoformat(),
        status="pending",
        sent_entry_alert=False,
        entry_time=None,
        exit_time=None,
        pnl_points=None,
        bars_held=0,
    )


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def resolve_same_candle(hit_sl: bool, hit_tp: bool) -> Optional[str]:
    if not (hit_sl or hit_tp):
        return None
    if hit_sl and hit_tp:
        return "loss" if CONSERVATIVE_SAME_CANDLE else "win"
    return "loss" if hit_sl else "win"


def manage_all_signals(m1: pd.DataFrame) -> None:
    with state_lock:
        raw_signals = BOT_STATE.get("signals", []).copy()

    if not raw_signals:
        return

    closed_df = m1.copy()
    if closed_df.empty:
        return

    updated: List[Dict[str, Any]] = []

    for raw in raw_signals:
        sig = ManagedSignal(**raw)

        if sig.status in {"win", "loss", "expired", "timeout"}:
            updated.append(asdict(sig))
            continue

        bos_time = pd.Timestamp(sig.bos_time)
        expires_at = pd.Timestamp(sig.expires_at)

        # ----------------------------------------
        # pending -> look for entry
        # ----------------------------------------
        if sig.status == "pending":
            entry_row = None

            for _, r in closed_df.iterrows():
                ts = pd.Timestamp(r["timestamp"])

                if ts <= bos_time:
                    continue
                if ts > expires_at:
                    break

                if float(r["low"]) <= sig.entry <= float(r["high"]):
                    entry_row = r
                    break

            if entry_row is not None:
                sig.status = "active"
                sig.entry_time = pd.Timestamp(entry_row["timestamp"]).isoformat()
                sig.bars_held = 0

                if not sig.sent_entry_alert:
                    send_telegram_message(
                        "📍 <b>ENTRY ALERT — XAUUSD SMC</b>\n"
                        f"Signal ID: <code>{esc_html(sig.signal_id)}</code>\n"
                        f"Direction: <b>{esc_html(sig.direction.upper())}</b>\n"
                        f"Entry time: <code>{esc_html(fmt_ts(sig.entry_time))}</code>\n"
                        f"Entry: <code>{esc_html(sig.entry)}</code>\n"
                        f"SL: <code>{esc_html(sig.sl)}</code>\n"
                        f"TP: <code>{esc_html(sig.tp)}</code>\n"
                        f"RR: <code>1:{esc_html(get_current_rr())}</code>"
                    )
                    sig.sent_entry_alert = True

            else:
                last_closed_ts = pd.Timestamp(closed_df.iloc[-1]["timestamp"])
                if last_closed_ts > expires_at:
                    sig.status = "expired"
                    sig.pnl_points = 0.0

                    send_telegram_message(
                        "⌛ <b>Signal expired</b>\n"
                        f"Signal ID: <code>{esc_html(sig.signal_id)}</code>\n"
                        f"Direction: <b>{esc_html(sig.direction.upper())}</b>\n"
                        "Entry was not triggered before expiry."
                    )

        # ----------------------------------------
        # active -> look for result
        # ----------------------------------------
        if sig.status == "active" and sig.entry_time:
            entry_time = pd.Timestamp(sig.entry_time)
            active_rows = closed_df[closed_df["timestamp"] >= entry_time].copy()

            for _, r in active_rows.iterrows():
                ts = pd.Timestamp(r["timestamp"])
                hi = float(r["high"])
                lo = float(r["low"])

                sig.bars_held = int((active_rows[active_rows["timestamp"] <= ts]).shape[0])

                if sig.direction == "long":
                    hit_sl = lo <= sig.sl
                    hit_tp = hi >= sig.tp
                    outcome = resolve_same_candle(hit_sl, hit_tp)

                    if outcome:
                        sig.status = outcome
                        sig.exit_time = ts.isoformat()
                        sig.pnl_points = round5(sig.tp - sig.entry) if outcome == "win" else round5(-(sig.entry - sig.sl))

                        send_telegram_message(
                            ("✅ " if outcome == "win" else "❌ ") +
                            "<b>RESULT — XAUUSD SMC</b>\n"
                            f"Signal ID: <code>{esc_html(sig.signal_id)}</code>\n"
                            "Direction: <b>LONG</b>\n"
                            f"Entry time: <code>{esc_html(fmt_ts(sig.entry_time))}</code>\n"
                            f"Exit time: <code>{esc_html(fmt_ts(sig.exit_time))}</code>\n"
                            f"Entry: <code>{esc_html(sig.entry)}</code>\n"
                            f"SL: <code>{esc_html(sig.sl)}</code>\n"
                            f"TP: <code>{esc_html(sig.tp)}</code>\n"
                            f"Outcome: <b>{esc_html(sig.status.upper())}</b>\n"
                            f"PnL points: <code>{esc_html(sig.pnl_points)}</code>"
                        )
                        break

                else:
                    hit_sl = hi >= sig.sl
                    hit_tp = lo <= sig.tp
                    outcome = resolve_same_candle(hit_sl, hit_tp)

                    if outcome:
                        sig.status = outcome
                        sig.exit_time = ts.isoformat()
                        sig.pnl_points = round5(sig.entry - sig.tp) if outcome == "win" else round5(-(sig.sl - sig.entry))

                        send_telegram_message(
                            ("✅ " if outcome == "win" else "❌ ") +
                            "<b>RESULT — XAUUSD SMC</b>\n"
                            f"Signal ID: <code>{esc_html(sig.signal_id)}</code>\n"
                            "Direction: <b>SHORT</b>\n"
                            f"Entry time: <code>{esc_html(fmt_ts(sig.entry_time))}</code>\n"
                            f"Exit time: <code>{esc_html(fmt_ts(sig.exit_time))}</code>\n"
                            f"Entry: <code>{esc_html(sig.entry)}</code>\n"
                            f"SL: <code>{esc_html(sig.sl)}</code>\n"
                            f"TP: <code>{esc_html(sig.tp)}</code>\n"
                            f"Outcome: <b>{esc_html(sig.status.upper())}</b>\n"
                            f"PnL points: <code>{esc_html(sig.pnl_points)}</code>"
                        )
                        break

                if sig.bars_held >= MAX_HOLD_BARS:
                    sig.status = "timeout"
                    sig.exit_time = ts.isoformat()
                    sig.pnl_points = 0.0

                    send_telegram_message(
                        "⌛ <b>Trade timeout — XAUUSD SMC</b>\n"
                        f"Signal ID: <code>{esc_html(sig.signal_id)}</code>\n"
                        f"Direction: <b>{esc_html(sig.direction.upper())}</b>\n"
                        "Trade closed due to max holding bars without TP/SL hit."
                    )
                    break

        updated.append(asdict(sig))

    keep: List[Dict[str, Any]] = []
    current_now = now_utc()

    for s in updated:
        status = s.get("status")
        if status in {"win", "loss", "expired", "timeout"}:
            ref_ts = s.get("exit_time") or s.get("bos_time")
            if ref_ts is not None:
                ts = pd.Timestamp(ref_ts)
                if current_now - ts <= pd.Timedelta(days=3):
                    keep.append(s)
        else:
            keep.append(s)

    with state_lock:
        BOT_STATE["signals"] = keep


# =========================================================
# STATUS / COMMANDS
# =========================================================
def get_status_text() -> str:
    with state_lock:
        enabled = BOT_STATE["enabled"]
        rr = BOT_STATE["rr"]
        signals = BOT_STATE.get("signals", [])

    pending_count = sum(1 for s in signals if s.get("status") == "pending")
    active_count = sum(1 for s in signals if s.get("status") == "active")
    win_count = sum(1 for s in signals if s.get("status") == "win")
    loss_count = sum(1 for s in signals if s.get("status") == "loss")
    expired_count = sum(1 for s in signals if s.get("status") == "expired")
    timeout_count = sum(1 for s in signals if s.get("status") == "timeout")

    return (
        "📊 <b>SMC bot status</b>\n"
        f"Enabled: <code>{esc_html(enabled)}</code>\n"
        f"RR(active): <code>1:{esc_html(rr)}</code>\n"
        f"Min SL distance: <code>&gt;{esc_html(MIN_STOP_DISTANCE)}</code>\n"
        f"Pending signals: <code>{esc_html(pending_count)}</code>\n"
        f"Active trades: <code>{esc_html(active_count)}</code>\n"
        f"Wins tracked: <code>{esc_html(win_count)}</code>\n"
        f"Losses tracked: <code>{esc_html(loss_count)}</code>\n"
        f"Expired tracked: <code>{esc_html(expired_count)}</code>\n"
        f"Timeout tracked: <code>{esc_html(timeout_count)}</code>\n"
        f"Tracked items: <code>{esc_html(len(signals))}</code>\n"
        f"Command polling: <code>{esc_html(ENABLE_COMMAND_POLLING)}</code>"
    )


def handle_command(text: str) -> None:
    cmd = text.strip().split()[0].lower()

    if cmd == "/startbt":
        with state_lock:
            BOT_STATE["enabled"] = True
        send_telegram_message("✅ <b>SMC bot running</b>\n" + get_status_text())
        return

    if cmd == "/stopbt":
        with state_lock:
            BOT_STATE["enabled"] = False
        send_telegram_message("🛑 <b>SMC bot stopped</b>\n" + get_status_text())
        return

    if cmd == "/statusbt":
        send_telegram_message(get_status_text())
        return

    if cmd == "/setrr":
        parts = text.strip().split()
        if len(parts) < 2:
            send_telegram_message("Usage: <code>/setrr 3</code>")
            return

        try:
            rr = float(parts[1])
            if rr <= 0:
                raise ValueError
        except Exception:
            send_telegram_message("❌ Invalid RR value.")
            return

        with state_lock:
            BOT_STATE["rr"] = rr
        send_telegram_message(f"✅ RR updated to <code>1:{esc_html(rr)}</code>")
        return

    if cmd == "/testmsg":
        send_telegram_message("✅ <b>Bot command system is working.</b>")
        return

    if cmd == "/resetbot":
        with state_lock:
            BOT_STATE["enabled"] = False
            BOT_STATE["rr"] = DEFAULT_RR
            BOT_STATE["signals"] = []
            BOT_STATE["seen_signal_ids"] = []
            BOT_STATE["last_update_id"] = 0
        send_telegram_message("♻️ <b>Bot state cleared.</b>")
        return

    send_telegram_message(
        "Unknown command.\n\n"
        "Available commands:\n"
        "<code>/startbt</code>\n"
        "<code>/stopbt</code>\n"
        "<code>/statusbt</code>\n"
        "<code>/setrr 3</code>\n"
        "<code>/testmsg</code>\n"
        "<code>/resetbot</code>"
    )


# =========================================================
# COMMAND LOOP
# =========================================================
def command_loop() -> None:
    print("Telegram command loop started", flush=True)

    while True:
        try:
            with state_lock:
                offset = BOT_STATE["last_update_id"] + 1

            data = get_updates(offset=offset, timeout=20)
            if not data.get("ok"):
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                update_id = int(upd["update_id"])

                with state_lock:
                    BOT_STATE["last_update_id"] = max(BOT_STATE["last_update_id"], update_id)

                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = str(msg.get("text", "")).strip()

                if not text.startswith("/"):
                    continue
                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                try:
                    handle_command(text)
                except Exception as e:
                    send_telegram_message(f"❌ Command error: <code>{esc_html(e)}</code>")

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text if e.response is not None else str(e)

            if status == 409:
                print("409 conflict: another polling process is using this bot token.", flush=True)
                print("Stop all duplicate bot instances. Only one polling bot can use a token.", flush=True)
            else:
                print(f"Command loop HTTP error: {status} {body}", flush=True)

            time.sleep(5)

        except Exception as e:
            print(f"Command loop error: {e}", flush=True)
            time.sleep(3)


# =========================================================
# SIGNAL LOOP
# =========================================================
def signal_loop() -> None:
    print("Signal loop started", flush=True)

    while True:
        try:
            with state_lock:
                enabled = BOT_STATE["enabled"]

            if not enabled:
                time.sleep(2)
                continue

            m1 = get_twelvedata_candles(SYMBOL, INTERVAL, LOOKBACK_BARS)
            if len(m1) < 30:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            manage_all_signals(m1)
            signal = detect_smc_signal_latest_closed(m1)

            if signal is not None:
                with state_lock:
                    seen = set(BOT_STATE.get("seen_signal_ids", []))
                    existing_ids = {s["signal_id"] for s in BOT_STATE.get("signals", [])}

                    if signal.signal_id not in seen and signal.signal_id not in existing_ids:
                        BOT_STATE["signals"].append(asdict(signal))
                        BOT_STATE["seen_signal_ids"].append(signal.signal_id)
                        BOT_STATE["seen_signal_ids"] = BOT_STATE["seen_signal_ids"][-2000:]

                        send_telegram_message(
                            "🚨 <b>LIVE SIGNAL DETECTED — XAUUSD SMC</b>\n"
                            f"Signal ID: <code>{esc_html(signal.signal_id)}</code>\n"
                            f"Direction: <b>{esc_html(signal.direction.upper())}</b>\n"
                            f"BOS time: <code>{esc_html(fmt_ts(signal.bos_time))}</code>\n"
                            f"OB time: <code>{esc_html(fmt_ts(signal.ob_time))}</code>\n"
                            f"Zone: <code>{esc_html(signal.zone_low)} - {esc_html(signal.zone_high)}</code>\n"
                            f"Entry: <code>{esc_html(signal.entry)}</code>\n"
                            f"SL: <code>{esc_html(signal.sl)}</code>\n"
                            f"TP: <code>{esc_html(signal.tp)}</code>\n"
                            f"RR: <code>1:{esc_html(get_current_rr())}</code>\n"
                            f"SL distance: <code>{esc_html(signal.risk)}</code>\n"
                            f"Expiry: <code>{esc_html(fmt_ts(signal.expires_at))}</code>"
                        )

            time.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            print(f"Signal loop error: {e}", flush=True)
            time.sleep(5)


# =========================================================
# MAIN
# =========================================================
def main() -> None:
    require_env()

    print("===================================================", flush=True)
    print("XAUUSD SMC TELEGRAM BOT STARTING", flush=True)
    print("===================================================", flush=True)
    print(f"Process ID             : {os.getpid()}", flush=True)
    print(f"PORT                   : {PORT}", flush=True)
    print(f"SYMBOL                 : {SYMBOL}", flush=True)
    print(f"INTERVAL               : {INTERVAL}", flush=True)
    print(f"DEFAULT_RR             : {DEFAULT_RR}", flush=True)
    print(f"MIN_STOP_DISTANCE      : {MIN_STOP_DISTANCE}", flush=True)
    print(f"PRICE_BUFFER           : {PRICE_BUFFER}", flush=True)
    print(f"LOOKBACK_BARS          : {LOOKBACK_BARS}", flush=True)
    print(f"ENTRY_WAIT_BARS        : {ENTRY_WAIT_BARS}", flush=True)
    print(f"MAX_HOLD_BARS          : {MAX_HOLD_BARS}", flush=True)
    print(f"CHECK_INTERVAL_SECONDS : {CHECK_INTERVAL_SECONDS}", flush=True)
    print(f"ENABLE_COMMAND_POLLING : {ENABLE_COMMAND_POLLING}", flush=True)

    print_webhook_info()
    clear_webhook()
    print_webhook_info()

    if ENABLE_COMMAND_POLLING:
        t1 = threading.Thread(target=command_loop, daemon=True)
        t1.start()
    else:
        print("Command polling disabled.", flush=True)

    t2 = threading.Thread(target=signal_loop, daemon=True)
    t2.start()

    send_startup_message_once()

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()