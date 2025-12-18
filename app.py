from flask import Flask, request, jsonify
import requests
import os
import time
import json
import tempfile
import logging
from datetime import datetime, date, timedelta, time as dtime

# ==================================================
# LOGGING
# ==================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("DHAN_ENGINE")

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
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_system_positions(state):
    fd, tmp = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def persist_system_state(system_id, state):
    SYSTEM_POSITIONS[system_id] = state
    save_system_positions(SYSTEM_POSITIONS)
    log.info(f"[STATE] Persisted {system_id}")

def remove_system_state(system_id):
    if system_id in SYSTEM_POSITIONS:
        del SYSTEM_POSITIONS[system_id]
        save_system_positions(SYSTEM_POSITIONS)
        log.info(f"[STATE] Removed {system_id}")

SYSTEM_POSITIONS = load_system_positions()

# ==================================================
# DHAN AUTH HELPERS
# ==================================================
def dhan_headers():
    return {
        "access-token": os.getenv("DHAN_ACCESS_TOKEN"),
        "client-id": os.getenv("DHAN_CLIENT_ID"),
        "Content-Type": "application/json"
    }

# ==================================================
# BROKER POSITIONS (REAL)
# ==================================================
def get_broker_positions():
    try:
        r = requests.get(
            "https://api.dhan.co/v2/positions",
            headers=dhan_headers(),
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"[BROKER][POSITIONS] {e}")
        return []

def broker_has_position(security_id, qty):
    for p in get_broker_positions():
        if str(p.get("securityId")) == str(security_id):
            return abs(int(p.get("netQty", 0))) >= qty
    return False

# ==================================================
# ORDER STATUS (REAL)
# ==================================================
def get_order_status(order_id):
    try:
        r = requests.get(
            f"https://api.dhan.co/v2/orders/{order_id}",
            headers=dhan_headers(),
            timeout=5
        )
        r.raise_for_status()
        return r.json().get("orderStatus")
    except Exception:
        return None

# ==================================================
# ORDER PLACEMENT (REAL)
# ==================================================
def place_order_with_checks(side, security_id, qty, ensure_fill=True):
    try:
        payload = {
            "transactionType": side,
            "exchangeSegment": "NSE_FNO",
            "productType": "NRML",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": qty,
            "disclosedQuantity": 0,
            "price": 0,
            "triggerPrice": "",
            "afterMarketOrder": False
        }

        r = requests.post(
            "https://api.dhan.co/v2/orders",
            headers=dhan_headers(), json=payload
        )
        r.raise_for_status()
        order_id = r.json().get("orderId")

        if not order_id:
            return {"placed": False}

        if ensure_fill:
            # poll until TRADED OR TIMEOUT
            for _ in range(5):
                time.sleep(1)
                status = get_order_status(order_id)
                if status == "TRADED":
                    return {"placed": True, "filled_completely": True}
                if status in ("REJECTED", "CANCELLED"):
                    return {"placed": False}
            return {"placed": False}
        return {"placed": True, "filled_completely": False}

    except Exception as e:
        log.error(f"[ORDER][ERROR] {e}")
        return {"placed": False}


# ==================================================
# ATM OPTION SELECTION (REAL)
# ==================================================
def get_contract_for_new_long(today):
    """
    REAL ATM option selector using Dhan Option Chain API
    """
    try:
        # 🔴 IMPORTANT: Underlying securityId for NIFTY (INDEX)
        # This must be correct. Commonly NIFTY = 13 in Dhan
        UNDERLYING_SCRIP = 13
        UNDERLYING_SEG = "IDX_I"

        payload = {
            "UnderlyingScrip": UNDERLYING_SCRIP,
            "UnderlyingSeg": UNDERLYING_SEG,
            "Expiry": ""   # empty = all expiries
        }

        url = "https://api.dhan.co/v2/optionchain"
        headers = dhan_headers()

        r = requests.post(url, headers=headers, json=payload, timeout=10)
        r.raise_for_status()

        data = r.json()
        oc = data.get("data", {}).get("oc", {})

        if not oc:
            log.error("[CONTRACT] Empty option chain response")
            return None

        # -----------------------------
        # Pick MONTHLY expiry (last date)
        # -----------------------------
        expiries = sorted(oc.keys())
        expiry = expiries[-1]

        expiry_data = oc[expiry]
        spot = float(expiry_data.get("last_price"))

        # -----------------------------
        # Pick ATM strike
        # -----------------------------
        strikes = [
            float(k) for k in expiry_data.keys()
            if k not in ("last_price",)
        ]

        atm_strike = min(strikes, key=lambda x: abs(x - spot))
        strike_data = expiry_data[str(atm_strike)]

        return {
            "expiry": date.fromisoformat(expiry),
            "strike": atm_strike,
            "call_security_id": strike_data["ce"]["securityId"],
            "put_security_id": strike_data["pe"]["securityId"]
        }

    except Exception as e:
        log.error(f"[CONTRACT][ERROR] {e}")
        return None



# ==================================================
# ENTER SYNTHETIC LONG
# ==================================================
def enter_synthetic_long(system_id, underlying, qty):
    contract = get_contract_for_new_long(date.today())
    if not contract:
        return {"entered": False}

    buy_call = place_order_with_checks("BUY", contract["call_security_id"], qty, True)
    if not buy_call.get("placed"):
        return {"entered": False}

    sell_put = place_order_with_checks("SELL", contract["put_security_id"], qty, False)

    return {
        "entered": True,
        "underlying": underlying,
        "expiry": contract["expiry"].isoformat(),
        "strike": contract["strike"],
        "call_security_id": contract["call_security_id"],
        "put_security_id": contract["put_security_id"] if sell_put.get("placed") else None,
        "qty": qty,
    }

# ==================================================
# EXIT SYNTHETIC LONG
# ==================================================
def exit_synthetic_long(system_id, state):
    qty = state["qty"]
    call_sid = state["call_security_id"]
    put_sid = state.get("put_security_id")

    exited = False

    if put_sid and broker_has_position(put_sid, qty):
        place_order_with_checks("BUY", put_sid, qty, True)
        exited = True

    if broker_has_position(call_sid, qty):
        place_order_with_checks("SELL", call_sid, qty, True)
        exited = True

    if exited:
        remove_system_state(system_id)
        return {"exited": True}

    return {"exited": False}

# ==================================================
# ROLLOVER
# ==================================================
def handle_rollover_if_needed():
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if now_ist.time() < dtime(12, 30):
        return {}

    today = date.today()
    summary = {}

    for system_id, state in list(SYSTEM_POSITIONS.items()):
        if date.fromisoformat(state["expiry"]) != today:
            continue

        if exit_synthetic_long(system_id, state).get("exited"):
            res = enter_synthetic_long(system_id, state["underlying"], state["qty"])
            if res.get("entered"):
                persist_system_state(system_id, res)
                summary[system_id] = "ROLLED"

    return summary

# ==================================================
# WEBHOOK
# ==================================================
@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    data = request.json or {}
    signal = str(data.get("signal", "")).upper()
    system_id = data.get("system_id")
    underlying = data.get("underlying", "NIFTY")
    qty = int(data.get("qty", 1))  # 🔴 START WITH 1 ONLY

    if signal == "CHECK":
        return jsonify(handle_rollover_if_needed())

    if signal == "BUY":
        if system_id in SYSTEM_POSITIONS:
            return jsonify({"ignored": True})
        res = enter_synthetic_long(system_id, underlying, qty)
        if res.get("entered"):
            persist_system_state(system_id, res)
        return jsonify(res)

    if signal in ("SELL", "EXIT"):
        if system_id not in SYSTEM_POSITIONS:
            return jsonify({"ignored": True})
        return jsonify(exit_synthetic_long(system_id, SYSTEM_POSITIONS[system_id]))

    return jsonify({"ignored": True})

# ==================================================
# HEALTH
# ==================================================
@app.route("/health")
def health():
    return jsonify({"ok": True, "systems": list(SYSTEM_POSITIONS.keys())})

# ==================================================
# RUN
# ==================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
