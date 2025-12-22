from flask import Flask, request, jsonify
import requests
import os
import time
import json
import tempfile
import logging
from datetime import datetime, date, timedelta, time as dtime
from threading import Thread



# ==================================================
# OPTION CHAIN CACHE (RATE LIMIT PROTECTION)
# ==================================================
OPTION_CHAIN_CACHE = {
    "expiry": None,
    "spot": None,
    "data": None,
    "ts": 0
}

OPTION_CHAIN_TTL = 3  # seconds


# ==================================================
# ENTRY EXECUTION CONFIG
# ==================================================
SPREAD_LIMIT = 20          # max bid-ask spread allowed
RETRY_INTERVAL = 3        # seconds between retries
MAX_WAIT_SECONDS = 20     # total wait before abort
FALLBACK_OFFSETS = [0, 100, -100]  # strikes in 100s only

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

os.makedirs("/data", exist_ok=True)
STATE_FILE = "/data/system_positions.json"
SYSTEM_POSITIONS = {}
# 🔍 BOOT-TIME DISK CHECK (TEMPORARY DEBUG)
log.info(f"[BOOT] STATE_FILE exists = {os.path.exists(STATE_FILE)}")
log.info(f"[BOOT] STATE_FILE path = {STATE_FILE}")

try:
    files = os.listdir("/data")
except Exception as e:
    files = f"ERROR: {e}"

log.info(f"[BOOT] Files in /data = {files}")


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
    tmp_path = STATE_FILE + ".tmp"

    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)

    # Atomic replace on SAME filesystem
    os.replace(tmp_path, STATE_FILE)


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

def observe_order_status_async(order_id, tag="", polls=6, interval=1):
    def _poll():
        for _ in range(polls):
            time.sleep(interval)
            status = get_order_status(order_id)
            log.info(
                f"[ORDER][STATUS][OBSERVE][{tag}] "
                f"orderId={order_id} status={status}"
            )

    Thread(target=_poll, daemon=True).start()

# ==================================================
# DHAN AUTH HELPERS
# ==================================================
def dhan_headers():
    cid = os.getenv("DHAN_CLIENT_ID")
    token = os.getenv("DHAN_ACCESS_TOKEN")

    if not cid or not token:
        log.critical("[AUTH] Missing Dhan credentials in environment")
        raise RuntimeError("Missing Dhan credentials")

    return {
        "access-token": token,
        "client-id": cid,
        "Content-Type": "application/json"
    }


   
def ensure_dhan_auth():
    cid = os.getenv("DHAN_CLIENT_ID")
    token = os.getenv("DHAN_ACCESS_TOKEN")

    if not cid or not token:
        log.critical("[AUTH] Missing Dhan credentials")
        raise RuntimeError("Missing Dhan credentials")

    if len(token) < 50:
        log.critical("[AUTH] Invalid / expired Dhan access token")
        raise RuntimeError("Invalid Dhan access token")

    log.info("[AUTH] Dhan credentials present")



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
        if not r.ok:
            log.error(
                f"[ORDER][RESPONSE][{r.status_code}] {r.text}"
            )
            return {
                "placed": False,
                "status_code": r.status_code,
                "error": r.text
            }

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

        if not r.ok:
            log.error(f"[ORDER][RESPONSE][{r.status_code}] {r.text}")
            return None

        data = r.json()

        if isinstance(data, list) and data:
            status = data[0].get("orderStatus")
        elif isinstance(data, dict):
            status = data.get("orderStatus")
        else:
            status = None

        log.info(f"[ORDER][STATUS][POLL] orderId={order_id} status={status}")
        return status

    except Exception as e:
        log.error(f"[ORDER][STATUS][ERROR] orderId={order_id} err={e}")
        return None



# ==================================================
# ORDER PLACEMENT (REAL)
# ==================================================
def place_order_with_checks(side, security_id, qty):
    try:
        payload = {
            "dhanClientId": 1101700964,   # ❗ NO hardcoding
            "transactionType": side,
            "exchangeSegment": "NSE_FNO",
            "productType": "MARGIN",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": int(security_id),
            "quantity": int(qty),
            "disclosedQuantity": 0,
            "afterMarketOrder": False
        }

        log.error("[ORDER][DEBUG][PAYLOAD] " + json.dumps(payload))

        r = requests.post(
            "https://api.dhan.co/v2/orders",
            headers=dhan_headers(),
            json=payload,
            timeout=10
        )

        log.error(f"[ORDER][RESPONSE] status={r.status_code} body={r.text}")

        # 🔐 AUTH FAILURE — HARD STOP
        if r.status_code == 401:
            log.critical("[AUTH] Dhan token expired or unauthorized — blocking trade")
            return {
                "placed": False,
                "filled_completely": False,
                "reason": "AUTH_FAILED"
            }

        # ❌ ORDER FAILED
        if not r.ok:
            log.error(f"[ORDER][FAILED][{r.status_code}] {r.text}")
            return {
                "placed": False,
                "filled_completely": False,
                "status_code": r.status_code,
                "error": r.text
            }

        data = r.json()
        order_id = data.get("orderId")

        if not order_id:
            log.error("[ORDER] No orderId returned by Dhan")
            return {
                "placed": False,
                "filled_completely": False
            }

        

        return {
            "placed": True,
            "order_id": order_id
        }

    except Exception as e:
        log.exception("[ORDER][EXCEPTION]")
        return {
            "placed": False,
            "filled_completely": False,
            "exception": str(e)
        }


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
        if not r.ok:
            log.error(f"[ORDER][RESPONSE][{r.status_code}] {r.text}")
            return {"placed": False, "error": r.text}


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
    now = time.time()

    # --------------------------------------------------
    # ✅ RETURN CACHED DATA IF FRESH
    # --------------------------------------------------
    if (
        OPTION_CHAIN_CACHE["expiry"] == expiry_str
        and now - OPTION_CHAIN_CACHE["ts"] < OPTION_CHAIN_TTL
    ):
        log.info("[CHAIN][CACHE] Using cached option chain")
        return OPTION_CHAIN_CACHE["spot"], OPTION_CHAIN_CACHE["data"]

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
        if not r.ok:
            log.error(f"[ORDER][RESPONSE][{r.status_code}] {r.text}")
            return {"placed": False, "error": r.text}


        data = r.json().get("data", {})
        oc = data.get("oc")

        if not isinstance(oc, dict) or not oc:
            log.error(f"[CHAIN] Empty option chain for expiry {expiry_str}")
            return None, None

        spot = float(data.get("last_price", 0))
        if spot <= 0:
            log.error("[CHAIN] Invalid spot price")
            return None, None

        # --------------------------------------------------
        # ✅ UPDATE CACHE
        # --------------------------------------------------
        OPTION_CHAIN_CACHE.update({
            "expiry": expiry_str,
            "spot": spot,
            "data": oc,
            "ts": time.time()
        })

        log.info("[CHAIN][FETCH] Option chain refreshed from API")
        return spot, oc

    except Exception as e:
        log.error(f"[CHAIN][ERROR] {e}")
        return None, None

def get_bid_ask(opt):
    """
    Extract bid/ask from Dhan option-chain response
    """
    try:
        bid = float(opt.get("top_bid_price", 0))
        ask = float(opt.get("top_ask_price", 0))
        if bid > 0 and ask > 0:
            return bid, ask
    except Exception:
        pass
    return None, None

def spread_ok(sd):
    ce = sd.get("ce")
    pe = sd.get("pe")

    if not ce or not pe:
        return False, None, None

    ce_bid, ce_ask = get_bid_ask(ce)
    pe_bid, pe_ask = get_bid_ask(pe)

    if not ce_bid or not pe_bid:
        return False, None, None

    ce_spread = ce_ask - ce_bid
    pe_spread = pe_ask - pe_bid

    ok = ce_spread <= SPREAD_LIMIT and pe_spread <= SPREAD_LIMIT
    return ok, ce_spread, pe_spread


def enter_synthetic(system_id, expiry, spot, qty):

    ensure_dhan_auth()
    start_time = time.time()
    base_strike = round(spot / 100) * 100

    log.info(f"[ENTER] Spot={spot:.2f} BaseStrike={base_strike}")

    for offset in FALLBACK_OFFSETS:
        strike = base_strike + offset
        log.info(f"[ENTER] Trying strike {strike}")

        while time.time() - start_time <= MAX_WAIT_SECONDS:

            # 🔹 Fetch option chain ONLY here
            spot, oc = fetch_option_chain_for_expiry(expiry)
            if not spot or not oc:
                log.warning("[ENTER] Option chain fetch failed, backing off")
                time.sleep(RETRY_INTERVAL)
                continue
            # --------------------------------------------------
            # NORMALIZE STRIKE KEYS (Dhan uses '25900.000000')
            # --------------------------------------------------
            normalized_oc = {}

            for k, v in oc.items():
                try:
                    strike_int = int(round(float(k)))
                    normalized_oc[strike_int] = v
                except Exception:
                    continue

            oc = normalized_oc

            if not spot or not oc:
                log.warning("[ENTER] Option chain fetch failed, backing off")
                time.sleep(RETRY_INTERVAL)
                continue
            spot_100 = int(round(spot / 100) * 100)
            nearby = sorted([s for s in oc.keys() if abs(s - spot_100) <= 500])
            log.info(f"[DEBUG][STRIKES][NEAR ATM] {nearby}")

            sd = oc.get(strike)
            if not sd:
                log.warning(f"[ENTER][DEBUG] Strike {strike} not present in option chain")
                break

            # 🔍 DEBUG: Print raw CE/PE once
            ce = sd.get("ce")
            pe = sd.get("pe")

            log.info(
                f"[DEBUG][RAW][{system_id}] "
                f"Strike={strike} "
                f"CE={json.dumps(ce, indent=2)} "
                f"PE={json.dumps(pe, indent=2)}"
            )

            # Continue normal checks
            if not ce or not pe:
                log.warning(f"[ENTER] Strike {strike} missing CE or PE")
                break


            ok, ce_spread, pe_spread = spread_ok(sd)

            log.info(
                f"[SPREAD] Strike={strike} "
                f"CE={ce_spread} PE={pe_spread} OK={ok}"
            )

            # ✅ GOOD SPREAD → EXECUTE IMMEDIATELY
            if ok:
                ce_sid = sd["ce"]["security_id"]
                pe_sid = sd["pe"]["security_id"]

                buy_call = place_order_with_checks("BUY", ce_sid, qty)
                if not buy_call.get("placed"):
                    log.error("[ENTER] BUY CALL placement failed")
                    return None
                if buy_call.get("order_id"):
                    observe_order_status_async(
                        buy_call["order_id"],
                        "CALL_ENTRY"
                    )
                log.info("[ENTER] BUY CALL accepted, proceeding to PUT")
                
                sell_put = place_order_with_checks("SELL", pe_sid, qty)
                if not sell_put.get("placed"):
                    log.critical("[ENTER] PUT leg failed after CALL — MANUAL INTERVENTION REQUIRED")
                    return None
                if sell_put.get("order_id"):
                    observe_order_status_async(
                        sell_put["order_id"],
                        "PUT_ENTRY"
                    )
                log.info(
                    f"[ENTER][SUCCESS] Strike={strike} "
                    f"CE={ce_sid} PE={pe_sid}"
                )

                return {
                    "expiry": expiry,
                    "strike": strike,
                    "call_security_id": ce_sid,
                    "put_security_id": pe_sid if sell_put.get("placed") else None,
                    "qty": qty,
                    "status": "OPEN"
                }

            # ❌ BAD SPREAD → WAIT, THEN RETRY
            log.info(
                f"[WAIT] Spread too wide for strike {strike}, "
                f"retrying in {RETRY_INTERVAL}s"
            )
            time.sleep(RETRY_INTERVAL)

        log.warning(
            f"[ENTER] Spread not acceptable for strike {strike}, "
            "trying fallback"
        )

    log.warning("[ENTER][ABORT] No strike met spread criteria. No trade.")
    return None
    
def delayed_enter_synthetic(system_id, expiry, spot, qty):
    try:
        log.info(f"[ENTER][ASYNC][START] {system_id}")

        state = enter_synthetic(system_id, expiry, spot, qty)

        if state:
            # --------------------------------------------------
            # ✅ Persist state
            # --------------------------------------------------
            persist_system_state(system_id, state)

            # --------------------------------------------------
            # 🔍 Post-persist verification (DEBUG)
            # --------------------------------------------------
            log.info(f"[STATE][VERIFY] STATE_FILE exists = {os.path.exists(STATE_FILE)}")

            try:
                files = os.listdir("/data")
            except Exception as e:
                files = f"ERROR: {e}"

            log.info(f"[STATE][VERIFY] Files in /data = {files}")

            log.info(f"[ENTER][DONE] {system_id}")

        else:
            log.warning(f"[ENTER][SKIPPED] {system_id}")

    except Exception as e:
        log.exception(f"[ENTER][ERROR][{system_id}]")



def exit_synthetic(system_id, state):
    qty = state["qty"]

    # Exit PUT first
    if state.get("put_security_id"):
        log.info(f"[EXIT] Closing PUT {state['put_security_id']}")
        put_exit = place_order_with_checks(
            "BUY",
            state["put_security_id"],
            qty
        )
        if put_exit.get("order_id"):
            observe_order_status_async(
                put_exit["order_id"],
                "PUT_EXIT"
            )

    # Exit CALL
    log.info(f"[EXIT] Closing CALL {state['call_security_id']}")
    call_exit = place_order_with_checks(
        "SELL",
        state["call_security_id"],
        qty
    )
    if call_exit.get("order_id"):
        observe_order_status_async(
            call_exit["order_id"],
            "CALL_EXIT"
        )

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
            system_id, next_expiry, spot, state["qty"]
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

        spot, _ = fetch_option_chain_for_expiry(expiry)
        if not spot:
            return jsonify({"error": "Spot fetch failed"}), 500


        Thread(
            target=delayed_enter_synthetic,
            args=(system_id, expiry, spot, qty),
            daemon=True
        ).start()

        return jsonify({"status": "entry_processing"}), 200


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
# system_positions.json
# ==================================================
@app.route("/debug/state", methods=["GET"])
def debug_state():
    return jsonify({
        "systems": SYSTEM_POSITIONS,
        "count": len(SYSTEM_POSITIONS)
    })


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








































