import os
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

STARTUP_TIME_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
SERVICE_NAME = os.getenv("SERVICE_NAME", "xauusd_smc_web")


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": SERVICE_NAME,
        "type": "web",
        "startup_time_utc": STARTUP_TIME_UTC,
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": SERVICE_NAME,
        "type": "web",
        "startup_time_utc": STARTUP_TIME_UTC,
        "message": "Web service is running.",
    })


@app.get("/ping")
def ping():
    return jsonify({"ok": True, "message": "pong"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)