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
    """
    Places a synthetic future:
      BUY ATM CALL
      SELL ATM PUT

    Returns dict with:
      entered: bool
      expiry
      strike
      call_security_id
      put_security_id
    """

    t0 = time.time()
    log.info(f"[BUY][START] system_id={system_id} underlying={underlying} qty={qty}")

    try:
        # --------------------------------------------------
        # 1️⃣ Decide contract (near / next expiry)
        # --------------------------------------------------
        today = datetime.utcnow().date()
        contract = get_contract_for_new_long(today)

        if not contract:
            log.error("[BUY][ERROR] No contract available")
            return {"entered": False, "reason": "no_contract"}

        contract = ensure_contract_populated(contract)

        expiry = contract["expiry"]
        strike = contract["strike"]
        call_sid = contract["call_security_id"]
        put_sid = contract["put_security_id"]

        log.info(
            f"[BUY][CONTRACT] expiry={expiry} strike={strike} "
            f"CE={call_sid} PE={put_sid}"
        )

        # --------------------------------------------------
        # 2️⃣ BUY CALL (must succeed)
        # --------------------------------------------------
        buy_call = place_order_with_checks(
            side="BUY",
            security_id=call_sid,
            qty=qty,
            t0=t0,
            ensure_fill=True
        )

        if not buy_call.get("placed") or not buy_call.get("filled_completely"):
            log.error("[BUY][FAILED] BUY CALL leg failed")
            return {
                "entered": False,
                "reason": "buy_call_failed",
                "details": buy_call,
            }

        log.info("[BUY][CALL] BUY CALL successful")
        
        # --------------------------------------------------
        # 3️⃣ SELL PUT (best effort)
        # --------------------------------------------------
        sell_put = place_order_with_checks(
            side="SELL",
            security_id=put_sid,
            qty=qty,
            t0=t0,
            ensure_fill=False
        )

        if not sell_put.get("placed"):
            log.error("[BUY][CRITICAL] BUY CALL filled but SELL PUT failed")
            log.error("[BUY][MANUAL] Naked CALL position exists – intervention required")
        
            # IMPORTANT:
            # BUY CALL already created broker exposure
            # We MUST mark this system as OPEN in state
            return {
                "entered": True,            # ← THIS IS THE KEY CHANGE
                "partial": True,
                "underlying": underlying,
                "expiry": expiry.isoformat(),
                "strike": strike,
                "call_security_id": call_sid,
                "put_security_id": None,    # PUT leg missing
                "qty": qty,
                "warning": "SELL_PUT_FAILED"
            }

            
        log.info("[BUY][PUT] SELL PUT placed")

        # --------------------------------------------------
        # 4️⃣ SUCCESS
        # --------------------------------------------------
        log.info(
            f"[BUY][SUCCESS] system_id={system_id} "
            f"expiry={expiry} strike={strike}"
        )

        return {
            "entered": True,
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "strike": strike,
            "call_security_id": call_sid,
            "put_security_id": put_sid,
            "qty": qty,
        }

    except Exception as e:
        log.exception(f"[BUY][CRITICAL] Exception during BUY: {e}")
        return {
            "entered": False,
            "reason": "exception",
            "error": str(e),
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
