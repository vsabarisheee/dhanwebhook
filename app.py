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
# OPTION EXPIRY LIST (OFFICIAL DHAN API)
# ==================================================
def get_option_expiries(underlying_id, underlying_seg="IDX_I"):
    """
    Fetch list of valid expiries for the underlying from Dhan
    """
    try:
        payload = {
            "UnderlyingScrip": int(underlying_id),
            "UnderlyingSeg": underlying_seg
        }

        url = "https://api.dhan.co/v2/optionchain/expirylist"
        r = requests.post(
            url,
            headers=dhan_headers(),
            json=payload,
            timeout=10
        )
        r.raise_for_status()

        resp = r.json()

        expiries = resp.get("data")
        if not isinstance(expiries, list) or not expiries:
            log.error(f"[EXPIRY] Invalid expiry response: {resp}")
            return []

        log.info(f"[EXPIRY][LIST] {expiries}")
        return expiries

    except Exception as e:
        log.error(f"[EXPIRY][ERROR] {e}")
        return []

def get_monthly_expiries(expiries):
    """
    Filters only MONTHLY expiries (last expiry of each month)
    """
    monthly = {}
    for e in expiries:
        d = date.fromisoformat(e)
        key = (d.year, d.month)
        monthly[key] = e  # last one wins

    return sorted(monthly.values())

def is_monthly_expiry_today(monthly_expiries):
    today = date.today().isoformat()
    return today in monthly_expiries

def choose_entry_expiry(monthly_expiries):
    """
    If today is expiry → use next month
    Else → use current month
    """
    today = date.today()

    for e in monthly_expiries:
        d = date.fromisoformat(e)
        if d >= today:
            if d == today:
                idx = monthly_expiries.index(e)
                return monthly_expiries[idx + 1]
            return e

    return None




def fetch_option_chain_for_expiry(expiry_str):
    uid = os.getenv("NIFTY_UNDERLYING_ID")
    if not uid:
        log.error("[CONFIG] NIFTY_UNDERLYING_ID not set")
        return None, None

    payload = {
        "UnderlyingScrip": int(uid),
        "UnderlyingSeg": "IDX_I",
        "Expiry": expiry_str
    }

    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=dhan_headers(),
            json=payload,
            timeout=10
        )
        r.raise_for_status()

        data = r.json().get("data", {})
        oc = data.get("oc")

        if not isinstance(oc, dict) or not oc:
            log.error(f"[CHAIN] Empty option chain for expiry {expiry_str}")
            return None, None

        spot = float(data.get("last_price", 0))
        if spot <= 0:
            log.error("[CHAIN] Invalid spot price")
            return None, None

        return spot, oc

    except Exception as e:
        log.error(f"[CHAIN][ERROR] {e}")
        return None, None



def select_atm_strike(expiry_data, spot):
    strikes = [float(k) for k in expiry_data.keys() if k != "last_price"]
    rounded_atm = round(spot / 100) * 100

    candidate_strikes = sorted(
        strikes,
        key=lambda x: abs(x - rounded_atm)
    )

    MAX_SPREAD = 10

    for strike in candidate_strikes:
        sd = expiry_data.get(str(strike))
        if not sd:
            continue

        ce, pe = sd.get("ce"), sd.get("pe")
        if not ce or not pe:
            continue

        ce_bid, ce_ask = float(ce["bestBidPrice"]), float(ce["bestAskPrice"])
        pe_bid, pe_ask = float(pe["bestBidPrice"]), float(pe["bestAskPrice"])

        if (ce_ask - ce_bid) <= MAX_SPREAD and (pe_ask - pe_bid) <= MAX_SPREAD:
            return strike, ce["securityId"], pe["securityId"]

    return None, None, None


def enter_synthetic(system_id, expiry, spot, expiry_data, qty):
    strike, call_sid, put_sid = select_atm_strike(expiry_data, spot)
    if not strike:
        log.warning("[ENTER] No liquid ATM found (market likely closed)")
        return None

    buy_call = place_order_with_checks("BUY", call_sid, qty, True)
    if not buy_call.get("placed"):
        return None

    sell_put = place_order_with_checks("SELL", put_sid, qty, False)

    return {
        "expiry": expiry,
        "strike": strike,
        "call_security_id": call_sid,
        "put_security_id": put_sid if sell_put.get("placed") else None,
        "qty": qty,
        "status": "OPEN"
    }

def exit_synthetic(system_id, state):
    qty = state["qty"]
    exited = False

    # Exit PUT leg first (if exists)
    if state.get("put_security_id") and broker_has_position(state["put_security_id"], qty):
        place_order_with_checks("BUY", state["put_security_id"], qty, True)
        exited = True

    # Exit CALL leg
    if broker_has_position(state["call_security_id"], qty):
        place_order_with_checks("SELL", state["call_security_id"], qty, True)
        exited = True

    # 🔁 RECONCILIATION FIX
    if not exited:
        log.warning(
            f"[EXIT][RECONCILE] No broker position for {system_id} — assuming already closed"
        )
        return True

    return True


def handle_rollover():
    underlying_id = int(os.getenv("NIFTY_UNDERLYING_ID"))
    expiries = get_option_expiries(underlying_id)
    monthly = get_monthly_expiries(expiries)

    if not is_monthly_expiry_today(monthly):
        return

    next_expiry = choose_entry_expiry(monthly)
    if not next_expiry:
        log.error("[ROLLOVER] No next monthly expiry")
        return

    for system_id, state in list(SYSTEM_POSITIONS.items()):
        if state["expiry"] != date.today().isoformat():
            continue

        if not exit_synthetic(system_id, state):
            log.error(f"[ROLLOVER] Exit failed for {system_id}")
            continue

        spot, oc = fetch_option_chain_for_expiry(next_expiry)
        if not spot or not oc:
            log.error(f"[ROLLOVER] Failed to fetch chain for {system_id}")
            continue

        new_state = enter_synthetic(
            system_id, next_expiry, spot, oc, state["qty"]
        )

        if new_state:
            persist_system_state(system_id, new_state)
        else:
            log.error(f"[ROLLOVER] Re-entry failed for {system_id}")
            remove_system_state(system_id)


# ==================================================
# WEBHOOK
# ==================================================
@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    data = request.json or {}

    signal = str(data.get("signal", "")).upper()
    system_id = data.get("system_id")
    underlying = data.get("underlying", "NIFTY")
    qty = int(data.get("qty", 0))

    # -------------------------------
    # QTY VALIDATION
    # -------------------------------
    if qty <= 0 or qty % 75 != 0:
        return jsonify({"error": "Invalid qty. Must be multiple of 75"}), 400

    # -------------------------------
    # ROLLOVER CHECK
    # -------------------------------
    if signal == "CHECK":
        handle_rollover()
        return jsonify({"status": "checked"}), 200

    # -------------------------------
    # BUY SIGNAL
    # -------------------------------
    if signal == "BUY":

        # 🔒 DUPLICATE POSITION PROTECTION
        if system_id in SYSTEM_POSITIONS:
            log.warning(f"[BUY][DUPLICATE] {system_id} already has open position")
            return jsonify({"error": "Position already open"}), 409

        uid = os.getenv("NIFTY_UNDERLYING_ID")
        if not uid:
            return jsonify({"error": "NIFTY_UNDERLYING_ID not set"}), 500

        underlying_id = int(uid)

        expiries = get_option_expiries(underlying_id)
        monthly = get_monthly_expiries(expiries)

        if not monthly:
            return jsonify({"error": "No monthly expiries"}), 400

        expiry = choose_entry_expiry(monthly)
        if not expiry:
            return jsonify({"error": "No valid entry expiry"}), 400

        spot, expiry_data = fetch_option_chain_for_expiry(expiry)
        if not spot or not expiry_data:
            return jsonify({"error": "Option chain fetch failed"}), 500

        state = enter_synthetic(
            system_id, expiry, spot, expiry_data, qty
        )

        if state:
            persist_system_state(system_id, state)
            return jsonify({"status": "entered"}), 200

        return jsonify({"error": "entry_failed"}), 400

    # -------------------------------
    # SELL / EXIT SIGNAL
    # -------------------------------
    if signal in ("SELL", "EXIT"):
        log.info(f"[SIGNAL][EXIT] {system_id}")

        if system_id not in SYSTEM_POSITIONS:
            log.warning(f"[EXIT][IGNORED] {system_id} not found")
            return jsonify({"status": "ignored"}), 200

        state = SYSTEM_POSITIONS[system_id]

        exited = exit_synthetic(system_id, state)

        if exited:
            remove_system_state(system_id)
            log.info(f"[EXIT][SUCCESS] {system_id} closed and state cleared")
            return jsonify({"status": "exited"}), 200

        log.error(f"[EXIT][FAILED] {system_id} exit failed")
        return jsonify({"status": "exit_failed"}), 500

    # -------------------------------
    # UNKNOWN SIGNAL
    # -------------------------------
    return jsonify({"status": "ignored"}), 200



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
