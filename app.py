from flask import Flask, request, jsonify
import requests
import os
import time
import json
import tempfile
import struct
import csv
import math
import logging
from datetime import date, datetime, timedelta, time as dtime
from websocket import WebSocketTimeoutException
import websocket

# ==================================================
# LOGGING
# ==================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("TRADE_ENGINE")

# ==================================================
# APP & STATE
# ==================================================
app = Flask(__name__)

STATE_FILE = "/tmp/system_positions.json"
SYSTEM_POSITIONS = {}

# ==================================================
# STATE HELPERS
# ==================================================
def load_system_positions():
    if not os.path.exists(STATE_FILE):
        log.info("[STATE] No existing state file. Fresh start.")
        return {}

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            log.info(f"[STATE] Loaded systems: {list(data.keys())}")
            return data
    except Exception as e:
        log.error(f"[STATE] Failed to load state: {e}")
        return {}


def save_system_positions(state):
    try:
        fd, tmp = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
        log.info(f"[STATE] Saved systems: {list(state.keys())}")
    except Exception as e:
        log.error(f"[STATE] Failed to save state: {e}")


def persist_system_state(system_id, state):
    SYSTEM_POSITIONS[system_id] = state
    save_system_positions(SYSTEM_POSITIONS)
    log.info(f"[STATE] Persisted {system_id}")


def remove_system_state(system_id):
    if system_id in SYSTEM_POSITIONS:
        del SYSTEM_POSITIONS[system_id]
        save_system_positions(SYSTEM_POSITIONS)
        log.info(f"[STATE] Removed {system_id}")

# ==================================================
# LOAD STATE ON START
# ==================================================
SYSTEM_POSITIONS = load_system_positions()

# ==================================================
# PLACEHOLDER – ORDER EXECUTION
# (You will plug your existing Dhan logic here)
# ==================================================
def enter_synthetic_long(system_id, underlying, qty):
    log.info(f"[ORDER][ENTER] {system_id} {underlying} qty={qty}")
    return {
        "entered": True,
        "expiry": "2025-01-30",
        "strike": 22500,
        "call_security_id": "12345",
        "put_security_id": "67890",
    }


def exit_synthetic_long(system_id, state):
    log.info(f"[ORDER][EXIT] {system_id}")
    return {"exited": True}

# ==================================================
# ROLLOVER CHECK (SAFE PLACEHOLDER)
# ==================================================
def rollover_check(now_utc):
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    if now_ist.time() < dtime(12, 30):
        return None
    log.info("[ROLLOVER] Time gate passed")
    return None

# ==================================================
# WEBHOOK
# ==================================================
@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    t0 = time.time()
    data = request.get_json() or {}
    log.info(f"[TV] Payload: {data}")

    raw_signal = str(data.get("signal", "")).upper()
    system_id = str(data.get("system_id", "")).strip()
    underlying = str(data.get("underlying", "NIFTY")).upper()
    qty = int(data.get("qty", 75))

    if raw_signal in ("BUY", "SELL", "EXIT") and not system_id:
        return jsonify({"error": "system_id required"}), 400

    rollover_check(datetime.utcnow())

    # ---------------- CHECK ----------------
    if raw_signal == "CHECK":
        return jsonify({"status": "ok", "action": "CHECK"}), 200

    # ---------------- BUY ----------------
    elif raw_signal == "BUY":
        log.info(f"[SIGNAL][BUY] {system_id}")

        if system_id in SYSTEM_POSITIONS:
            log.warning(f"[BUY][IGNORED] {system_id} already open")
            return jsonify({"status": "ignored"}), 200

        res = enter_synthetic_long(system_id, underlying, qty)
        if not res.get("entered"):
            return jsonify({"status": "failed"}), 200

        state = {
            "underlying": underlying,
            "qty": qty,
            "expiry": res["expiry"],
            "strike": res["strike"],
            "call_security_id": res["call_security_id"],
            "put_security_id": res["put_security_id"],
            "entry_time": datetime.utcnow().isoformat(),
            "status": "OPEN",
        }

        persist_system_state(system_id, state)

        return jsonify({"status": "ok", "state": state}), 200

    # ---------------- SELL / EXIT ----------------
    elif raw_signal in ("SELL", "EXIT"):
        log.info(f"[SIGNAL][EXIT] {system_id}")

        if system_id not in SYSTEM_POSITIONS:
            log.warning(f"[EXIT][IGNORED] {system_id} not found")
            return jsonify({"status": "ignored"}), 200

        state = SYSTEM_POSITIONS[system_id]
        res = exit_synthetic_long(system_id, state)

        if res.get("exited"):
            remove_system_state(system_id)

        return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ignored"}), 400

# ==================================================
# ADMIN & HEALTH
# ==================================================
@app.route("/admin/reset-system", methods=["POST"])
def reset_system():
    data = request.get_json() or {}
    system_id = data.get("system_id")
    if not system_id:
        return jsonify({"error": "system_id required"}), 400
    remove_system_state(system_id)
    return jsonify({"status": "ok"}), 200


@app.route("/health")
def health():
    return jsonify({"ok": True, "systems": list(SYSTEM_POSITIONS.keys())})


@app.route("/")
def home():
    return "Dhan Trading Bot running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
