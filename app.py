from flask import Flask, request, jsonify
import requests
import os
import time
import json
import struct
from datetime import date, datetime
import csv
from websocket import WebSocketTimeoutException


import websocket  # for Full Market Depth WS

app = Flask(__name__)

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

# From your environment (Render "Environment" section)
# Must be your numeric Dhan client id (e.g. 1101700964), NOT the API key
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_API_KEY = os.getenv("DHAN_API_KEY")  # keep separate if you need it later
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

# Dhan v2 base URL
DHAN_BASE_URL = "https://api.dhan.co/v2"

# Paper / Live toggle
LIVE = False  # <-- set to True when you're 100% ready

# Turn on/off margin pre-check via /margincalculator
MARGIN_CHECK_ENABLED = False

# Product type:
#   "INTRADAY" -> intraday trades only
#   "CNC"      -> positional / carry forward for F&O
PRODUCT_TYPE = "CNC"   # you want positional

# Liquidity rules
MAX_SPREAD_POINTS = 15.0   # max allowed (ask - bid)
MIN_QTY_MULTIPLIER = 1.0  # both bid/ask qty should be >= qty * this

# Order polling settings for "ensure filled" logic
ORDER_FILL_MAX_WAIT = 15      # seconds
ORDER_FILL_POLL_INTERVAL = 1  # seconds

# NIFTY synthetic future contracts:
# We will AUTO-POPULATE this from Dhan instrument master + option chain
NIFTY_SYNTH_CONTRACTS = []  # will be filled dynamically

# Underlying / option metadata for NIFTY
NIFTY_UNDERLYING_SYMBOL = "NIFTY"
NIFTY_UNDERLYING_SEG = "IDX_I"  # as per Option Chain docs

# For Nifty 50 index, Option Chain docs use Security ID 13 as example.
# We'll use 13 as the underlying SecurityId for NIFTY.
NIFTY_UNDERLYING_SECURITY_ID = 13

NIFTY_OPTION_METADATA = {}  # expiry_date (date) -> list of rows (dicts) for that expiry

# Simple cache for Dhan postback info (in-memory)
ORDER_STATUS_CACHE = {}


# --------------------------------------------------
# BASIC HELPERS
# --------------------------------------------------

def get_field(row: dict, *names):
    """
    Safely fetch first non-empty field from a row by trying multiple column names.
    Helps handle slight variations in Dhan CSV headers.
    """
    for name in names:
        if name in row and row[name] not in (None, "", "NA"):
            return row[name]
    return None


def dhan_headers_json(include_client=False):
    """
    Common headers for JSON APIs.
    For Orders/Funds/Margin, Dhan requires at least access-token.
    Option Chain etc also need client-id.
    """
    headers = {
        "Content-Type": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
    }
    if include_client and DHAN_CLIENT_ID:
        headers["client-id"] = DHAN_CLIENT_ID
    return headers


def load_nifty_option_metadata():
    """
    Load NIFTY index options (monthly only) from Dhan detailed master CSV.

    - Filters: INSTRUMENT == OPTIDX, UNDERLYING_SYMBOL == NIFTY, OPTION_TYPE in {CE, PE}, EXPIRY_FLAG == M
    - Populates:
        * NIFTY_OPTION_METADATA: { expiry_date -> [ rows ] }
        * NIFTY_UNDERLYING_SECURITY_ID
        * NIFTY_SYNTH_CONTRACTS: shell contracts with only name + expiry
    """
    global NIFTY_OPTION_METADATA, NIFTY_UNDERLYING_SECURITY_ID, NIFTY_SYNTH_CONTRACTS

    if NIFTY_OPTION_METADATA:
        return  # already loaded

    try:
        print("[INIT] Loading NIFTY option metadata from Dhan master CSV...")
        resp = requests.get(
            "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
            timeout=15,
        )
        resp.raise_for_status()

        lines = resp.text.splitlines()
        reader = csv.DictReader(lines)

        option_rows = []
        for row in reader:
            instrument = get_field(row, "INSTRUMENT")
            underlying_symbol = get_field(row, "UNDERLYING_SYMBOL")
            opt_type = get_field(row, "OPTION_TYPE")
            expiry_flag = get_field(row, "EXPIRY_FLAG")

            if instrument != "OPTIDX":
                continue
            if underlying_symbol != NIFTY_UNDERLYING_SYMBOL:
                continue
            if opt_type not in ("CE", "PE"):
                continue
            if expiry_flag != "M":
                continue  # only monthly expiry

            option_rows.append(row)

        if not option_rows:
            print("[INIT] No NIFTY options found in master file – check filters / CSV structure.")
            return

        # Build expiry-wise buckets
        meta = {}
        underlying_ids = set()

        for row in option_rows:
            exp_str = get_field(row, "SM_EXPIRY_DATE", "EXPIRY_DATE", "EXPIRY")
            if not exp_str:
                continue

            # Try a couple of common date formats
            exp_str = exp_str.strip()
            exp_dt = None
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
                try:
                    exp_dt = datetime.strptime(exp_str, fmt).date()
                    break
                except Exception:
                    continue

            if not exp_dt:
                continue

            meta.setdefault(exp_dt, []).append(row)

            u_id = get_field(row, "UNDERLYING_SECURITY_ID", "UNDERLYING_SMST_SECURITY_ID")
            if u_id:
                try:
                    underlying_ids.add(int(float(u_id)))
                except Exception:
                    pass

        if not meta:
            print("[INIT] No expiry buckets formed for NIFTY options.")
            return

        NIFTY_OPTION_METADATA = meta

                # If not already set, derive underlying security id from option rows.
        # But for NIFTY index we usually hardcode 13, so don't override if already set.
        if underlying_ids and NIFTY_UNDERLYING_SECURITY_ID is None:
            NIFTY_UNDERLYING_SECURITY_ID = sorted(underlying_ids)[0]

        print(f"[INIT] NIFTY UNDERLYING_SECURITY_ID={NIFTY_UNDERLYING_SECURITY_ID}")


        # Create shell contracts (expiry only, strike & SIDs later)
        contracts = []
        for exp in sorted(meta.keys()):
            contracts.append(
                {
                    "name": f"NIFTY_SYN_{exp.strftime('%Y%m%d')}",
                    "expiry": exp,
                    "strike": None,
                    "call_security_id": None,
                    "put_security_id": None,
                }
            )

        NIFTY_SYNTH_CONTRACTS = contracts
        print(f"[INIT] Built {len(NIFTY_SYNTH_CONTRACTS)} NIFTY monthly synthetic slots.")

    except Exception as e:
        print("[EXCEPTION] load_nifty_option_metadata:", e)


def pick_atm_for_expiry(expiry: date):
    """
    For a given expiry, use Option Chain API to:
      1) Fetch NIFTY spot (last_price / underlyingValue / ltp)
      2) Find strike closest to spot from NIFTY_OPTION_METADATA
      3) Return (atm_strike, call_sid, put_sid)
    """
    load_nifty_option_metadata()
    if not NIFTY_OPTION_METADATA:
        raise RuntimeError("NIFTY_OPTION_METADATA is empty – cannot pick ATM.")

    rows = NIFTY_OPTION_METADATA.get(expiry)
    if not rows:
        raise RuntimeError(f"No NIFTY options found for expiry {expiry}.")

    if NIFTY_UNDERLYING_SECURITY_ID is None:
        raise RuntimeError("NIFTY_UNDERLYING_SECURITY_ID is not set.")

    # 1) Call Option Chain for this expiry
    payload = {
        "UnderlyingScrip": int(NIFTY_UNDERLYING_SECURITY_ID),
        "UnderlyingSeg": NIFTY_UNDERLYING_SEG,
        "Expiry": expiry.strftime("%Y-%m-%d"),
    }

    spot = None
    try:
        resp = requests.post(
            f"{DHAN_BASE_URL}/optionchain",
            headers=dhan_headers_json(include_client=True),
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            print("[ERROR] optionchain:", resp.status_code, resp.text)
        else:
            j = resp.json()
            data = j.get("data") or {}
            # Try a few possible keys for spot / underlying price
            spot = float(
                (data.get("last_price")
                 or data.get("underlyingValue")
                 or data.get("ltp")
                 or 0.0)
            )
            print(f"[ATM] NIFTY spot for {expiry}: {spot}")
    except Exception as e:
        print("[EXCEPTION] optionchain:", e)
        spot = None

    # 2) Collect all strikes for this expiry
    strikes = set()
    for r in rows:
        sp = get_field(r, "STRIKE_PRICE", "STRIKE")
        if not sp:
            continue
        try:
            strikes.add(float(sp))
        except Exception:
            continue

    if not strikes:
        raise RuntimeError(f"No strikes found for expiry {expiry}.")

    strikes = sorted(s for s in strikes if s > 0)

    if spot and spot > 0:
        atm_strike = min(strikes, key=lambda s: abs(s - spot))
    else:
        # fallback: use middle strike
        atm_strike = strikes[len(strikes) // 2]

    print(f"[ATM] Chosen ATM strike {atm_strike} for expiry {expiry}")

    # 3) Find CE / PE security IDs for this strike
    call_sid = None
    put_sid = None

    for r in rows:
        sp = get_field(r, "STRIKE_PRICE", "STRIKE")
        if not sp:
            continue
        try:
            s_val = float(sp)
        except Exception:
            continue

        if s_val != atm_strike:
            continue

        opt_type = get_field(r, "OPTION_TYPE")
        sec_id = get_field(r, "SMST_SECURITY_ID", "SECURITY_ID")
        if not sec_id:
            continue

        sid_str = None
        try:
            sid_str = str(int(float(sec_id)))
        except Exception:
            sid_str = str(sec_id)

        if opt_type == "CE":
            call_sid = sid_str
        elif opt_type == "PE":
            put_sid = sid_str

    if not call_sid or not put_sid:
        raise RuntimeError(
            f"Could not find both CE & PE security IDs for ATM strike {atm_strike} on {expiry}"
        )

    print(
        f"[ATM] Expiry {expiry} ATM {atm_strike}: CE={call_sid}, PE={put_sid}"
    )

    return float(atm_strike), call_sid, put_sid


def ensure_contract_populated(contract: dict):
    """
    Make sure the given synthetic contract dict has:
      - strike
      - call_security_id
      - put_security_id

    If missing, derive from metadata + option chain.
    """
    if contract.get("call_security_id") and contract.get("put_security_id") and contract.get("strike"):
        return contract  # already done

    expiry = contract["expiry"]
    atm_strike, call_sid, put_sid = pick_atm_for_expiry(expiry)

    contract["strike"] = atm_strike
    contract["call_security_id"] = call_sid
    contract["put_security_id"] = put_sid

    return contract


def sorted_synth_contracts():
    """Return NIFTY synthetic contracts (auto-built) sorted by expiry."""
    load_nifty_option_metadata()
    return sorted(NIFTY_SYNTH_CONTRACTS, key=lambda c: c["expiry"])


def get_near_and_next_contract(today: date):
    """
    near: first contract whose expiry >= today
    next: contract after near, if exists
    """
    contracts = sorted_synth_contracts()
    if not contracts:
        return None, None

    near = None
    for c in contracts:
        if c["expiry"] >= today:
            near = c
            break

    if near is None:
        near = contracts[-1]
        return near, None

    idx = contracts.index(near)
    next_c = contracts[idx + 1] if idx + 1 < len(contracts) else None
    return near, next_c


def get_contract_for_new_long(today: date):
    """
    For NEW synthetic long:
    - If near expiry == today and next exists -> use next
    - Else use near-month

    Additionally:
    - Auto-populate strike + CE/PE security IDs for the chosen expiry
      using master CSV + Option Chain (ATM selection).
    """
    near, next_c = get_near_and_next_contract(today)
    if near is None:
        return None

    if near["expiry"] == today and next_c is not None:
        print(
            f"[INFO] Today is expiry for {near['name']} -> using {next_c['name']} for new synthetic long"
        )
        contract = next_c
    else:
        contract = near

    # ensure SIDs & strike are filled for this contract
    contract = ensure_contract_populated(contract)
    return contract


def get_positions():
    """GET /positions – all open positions for the day."""
    try:
        resp = requests.get(
            f"{DHAN_BASE_URL}/positions",
            headers=dhan_headers_json(),
            timeout=3,
        )
        if resp.status_code != 200:
            print("[ERROR] get_positions:", resp.status_code, resp.text)
            return []
        return resp.json()
    except Exception as e:
        print("[EXCEPTION] get_positions:", e)
        return []


# --------------------------------------------------
# FULL MARKET DEPTH (20 LEVEL) FOR LIQUIDITY CHECK
# --------------------------------------------------

def get_top_of_book_from_depth(security_id: str):
    """
    Uses Dhan Full Market Depth (20-level) WebSocket to fetch
    best bid & ask for a single NSE_FNO instrument.

    Returns:
      {
        "bid": {"price": float, "qty": int} or None,
        "ask": {"price": float, "qty": int} or None
      }
    """
    if not DHAN_CLIENT_ID or not DHAN_ACCESS_TOKEN:
        print("[WARN] Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN for depth feed")
        return {"bid": None, "ask": None}

    url = (
        "wss://depth-api-feed.dhan.co/twentydepth"
        f"?token={DHAN_ACCESS_TOKEN}"
        f"&clientId={DHAN_CLIENT_ID}"
        "&authType=2"
    )

    ws = None
    best_bid = None
    best_ask = None

    try:
        ws = websocket.create_connection(url, timeout=3)
        ws.settimeout(1.0)  # <-- add this: max 1 sec wait per recv
        
        # Subscribe to this one F&O instrument
        sub_msg = {
            "RequestCode": 23,  # Full Market Depth (20-level)
            "InstrumentCount": 1,
            "InstrumentList": [
                {
                    "ExchangeSegment": "NSE_FNO",
                    "SecurityId": str(security_id),
                }
            ],
        }
        ws.send(json.dumps(sub_msg))

        deadline = time.time() + 2.0  # wait up to 2 sec for data

        while time.time() < deadline and (best_bid is None or best_ask is None):
            try:
                frame = ws.recv()
            except WebSocketTimeoutException:
                print("[DEPTH] recv timeout, no data for this SID")
                break            
            if isinstance(frame, str):
                # Ignore any text frames
                continue

            buf = frame if isinstance(frame, (bytes, bytearray)) else bytes(frame)
            pos = 0
            n = len(buf)

            # Packets may be stacked one after another in same message.
            while pos + 12 <= n:
                # Header is 12 bytes, little-endian per Live Feed docs.
                # bytes 0-1: int16 length
                msg_len = struct.unpack("<h", buf[pos:pos + 2])[0]
                if msg_len <= 0 or pos + msg_len > n:
                    break

                feed_code = buf[pos + 2]  # 41 = Bid, 51 = Ask
                sec_id_msg = struct.unpack("<i", buf[pos + 4:pos + 8])[0]

                if str(sec_id_msg) != str(security_id):
                    pos += msg_len
                    continue

                # Depth starts at byte offset 12; each level is 16 bytes
                depth_buf = buf[pos + 12:pos + msg_len]
                if len(depth_buf) < 16:
                    pos += msg_len
                    continue

                # We only need the first level
                level = depth_buf[0:16]
                price = struct.unpack("<d", level[0:8])[0]
                qty = struct.unpack("<I", level[8:12])[0]
                # orders = struct.unpack("<I", level[12:16])[0]  # not used

                if feed_code == 41:  # Bid
                    best_bid = {"price": price, "qty": qty}
                elif feed_code == 51:  # Ask
                    best_ask = {"price": price, "qty": qty}

                pos += msg_len

            if best_bid is not None and best_ask is not None:
                break

    except Exception as e:
        print("[EXCEPTION] get_top_of_book_from_depth:", e)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    return {"bid": best_bid, "ask": best_ask}


def check_liquidity(security_id: str, qty: int):
    """
    Returns (ok, info_dict) based on:
      - both bid & ask exist
      - both bid/ask quantities >= qty * MIN_QTY_MULTIPLIER
      - spread <= MAX_SPREAD_POINTS
    Uses 20-level Full Market Depth WS.
    """
    book = get_top_of_book_from_depth(security_id)
    bid = book.get("bid")
    ask = book.get("ask")

    if not bid or not ask:
        return False, {"reason": "missing_bid_or_ask", "book": book}

    bid_price = float(bid.get("price", 0.0) or 0.0)
    bid_qty = int(bid.get("qty", 0) or 0)
    ask_price = float(ask.get("price", 0.0) or 0.0)
    ask_qty = int(ask.get("qty", 0) or 0)

    if bid_price <= 0 or ask_price <= 0:
        return False, {
            "reason": "invalid_bid_or_ask",
            "bid_price": bid_price,
            "ask_price": ask_price,
            "book": book,
        }

    spread = ask_price - bid_price
    ok_qty = (bid_qty >= qty * MIN_QTY_MULTIPLIER) and (
        ask_qty >= qty * MIN_QTY_MULTIPLIER
    )
    ok_spread = spread <= MAX_SPREAD_POINTS

    info = {
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread": spread,
        "ok_qty": ok_qty,
        "ok_spread": ok_spread,
    }

    ok = ok_qty and ok_spread
    return ok, info


# --------------------------------------------------
# FUNDS / MARGIN CHECK
# --------------------------------------------------

def check_margin(security_id: str, transaction_type: str, qty: int, price: float):
    """
    Uses POST /margincalculator to estimate margin and availableBalance.
    Returns (ok, response_json_or_text)
      ok = True if availableBalance >= totalMargin and insufficientBalance <= 0
    """
    if not MARGIN_CHECK_ENABLED:
        return True, {"skipped": True}

    if not DHAN_CLIENT_ID:
        print("[WARN] No DHAN_CLIENT_ID – cannot do margin calc, skipping check.")
        return True, {"skipped": True}

    payload = {
        "dhanClientId": DHAN_CLIENT_ID,
        "exchangeSegment": "NSE_FNO",
        "transactionType": transaction_type,
        "quantity": int(qty),
        "productType": PRODUCT_TYPE,
        "securityId": str(security_id),
        "price": float(price),
        "triggerPrice": 0.0,
    }

    try:
        resp = requests.post(
            f"{DHAN_BASE_URL}/margincalculator",
            headers=dhan_headers_json(),
            json=payload,
            timeout=3,
        )
        if resp.status_code != 200:
            print("[ERROR] margincalculator:", resp.status_code, resp.text)
            return False, resp.text

        j = resp.json()
        total_margin = float(j.get("totalMargin", 0.0))
        available_balance = float(j.get("availableBalance", 0.0))
        insuff = float(j.get("insufficientBalance", 0.0))

        ok = (available_balance >= total_margin) and (insuff <= 0.0)
        return ok, j
    except Exception as e:
        print("[EXCEPTION] margincalculator:", e)
        # fail open to avoid blocking because of network errors
        return True, {"error": str(e)}


# --------------------------------------------------
# ORDERS (v2) – place / get / wait for fill
# --------------------------------------------------

def _post_order(side: str, security_id: str, qty: int, correlation_id: str = None):
    """
    Low-level POST /orders.
    Returns (status_code, response_json_or_text)
    """
    if not DHAN_CLIENT_ID:
        raise RuntimeError("DHAN_CLIENT_ID missing; set env DHAN_CLIENT_ID or DHAN_API_KEY")

    if correlation_id is None:
        correlation_id = f"synth-{int(time.time() * 1000)}"

    payload = {
        "dhanClientId": DHAN_CLIENT_ID,
        "correlationId": correlation_id,
        "transactionType": side,
        "exchangeSegment": "NSE_FNO",
        "productType": PRODUCT_TYPE,
        "orderType": "MARKET",
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(qty),
        "disclosedQuantity": 0,
        "price": 0.0,
        "triggerPrice": 0.0,
        "afterMarketOrder": False,
        "amoTime": "",
        "boProfitValue": 0.0,
        "boStopLossValue": 0.0,
    }

    if not LIVE:
        print(f"[PAPER ORDER] Would {side} {qty} of {security_id} payload={payload}")
        # mimic Dhan response
        return 200, {"orderId": "PAPER", "orderStatus": "PAPER"}

    print(f"[LIVE ORDER] {side} {qty} of {security_id} payload={payload}")
    resp = requests.post(
        f"{DHAN_BASE_URL}/orders",
        headers=dhan_headers_json(),
        json=payload,
        timeout=3,
    )
    try:
        j = resp.json()
    except Exception:
        j = resp.text
    return resp.status_code, j


def get_order(order_id: str):
    """
    GET /orders/{order-id}.
    """
    if order_id == "PAPER":
        # synthetic response
        return {"orderId": "PAPER", "orderStatus": "PAPER", "filledQty": 0, "quantity": 0}

    # Prefer latest postback info if present
    if order_id in ORDER_STATUS_CACHE:
        return ORDER_STATUS_CACHE[order_id]

    try:
        resp = requests.get(
            f"{DHAN_BASE_URL}/orders/{order_id}",
            headers=dhan_headers_json(),
            timeout=3,
        )
        if resp.status_code != 200:
            print("[ERROR] get_order:", resp.status_code, resp.text)
            return None
        return resp.json()
    except Exception as e:
        print("[EXCEPTION] get_order:", e)
        return None


def wait_for_fill(order_id: str):
    """
    Polls GET /orders/{order-id} until order fully traded or timeout.
    Returns (filled_completely: bool, last_status_dict)
    """
    if order_id == "PAPER":
        return True, {"orderStatus": "PAPER"}

    deadline = time.time() + ORDER_FILL_MAX_WAIT
    last_status = None

    while time.time() < deadline:
        st = get_order(order_id)
        if not st:
            time.sleep(ORDER_FILL_POLL_INTERVAL)
            continue

        last_status = st
        status = st.get("orderStatus")
        filled_qty = st.get("filled_qty") or st.get("filledQty") or 0
        qty = st.get("quantity") or 0

        print(f"[ORDER POLL] {order_id} status={status}, filled={filled_qty}/{qty}")

        if status in ("TRADED", "PART_TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
            if status == "TRADED" and filled_qty >= qty:
                return True, st
            else:
                return False, st

        time.sleep(ORDER_FILL_POLL_INTERVAL)

    return False, last_status


def place_order_with_checks(
    side: str,
    security_id: str,
    qty: int,
    ensure_fill: bool = False,
):
    """
    1) Check liquidity via Full Depth (spread + depth).
       - If not OK, wait 5 sec and re-check.
       - If still not OK, DO NOT place order.
    2) Check margin via /margincalculator (if enabled).
    3) Place order (PAPER or LIVE).
    4) If ensure_fill=True, poll until fully traded or timeout.
    """
    print(f"[LIQUIDITY] Checking {side} {qty} on {security_id}...")
    ok, info = check_liquidity(security_id, qty)
    print("[LIQUIDITY] First check:", info)

        if not ok:
        # If there was literally no valid bid/ask, don't waste time re-checking
        if info.get("reason") in ("invalid_bid_or_ask", "missing_bid_or_ask"):
            print("[LIQUIDITY] Invalid or missing bid/ask -> NOT placing order (no retry).")
            return {
                "placed": False,
                "liquidity_ok": False,
                "liquidity_info": info,
            }

        print("[LIQUIDITY] Not sufficient, waiting 5 seconds then re-check...")
        time.sleep(5)
        ok2, info2 = check_liquidity(security_id, qty)
        ...

        print("[LIQUIDITY] Second check:", info2)
        if not ok2:
            print("[LIQUIDITY] Still poor -> NOT placing order.")
            return {
                "placed": False,
                "liquidity_ok": False,
                "liquidity_info": info2,
            }
        info = info2

    # Approx price for margin check
    bid_price = info.get("bid_price", 0.0) or 0.0
    ask_price = info.get("ask_price", 0.0) or 0.0
    if side == "BUY":
        approx_price = ask_price
    else:
        approx_price = bid_price

    margin_info = None    # noqa: F841
    if approx_price > 0:
        margin_ok, margin_info = check_margin(security_id, side, qty, approx_price)
        print("[MARGIN] Check:", margin_info)
        if not margin_ok:
            print("[MARGIN] Insufficient -> NOT placing order.")
            return {
                "placed": False,
                "liquidity_ok": True,
                "liquidity_info": info,
                "margin_ok": False,
                "margin_info": margin_info,
            }

    status_code, resp = _post_order(side, security_id, qty)
    order_id = resp.get("orderId") if isinstance(resp, dict) else None

    result = {
        "placed": status_code in (200, 201),
        "status_code": status_code,
        "resp": resp,
        "order_id": order_id,
        "liquidity_ok": True,
        "liquidity_info": info,
        "margin_ok": True,
        "margin_info": margin_info,
    }

    if ensure_fill and order_id:
        filled, st = wait_for_fill(order_id)
        result["filled_completely"] = filled
        result["final_status"] = st
    else:
        result["filled_completely"] = not ensure_fill
        result["final_status"] = None

    return result


# --------------------------------------------------
# SYNTHETIC FUTURE (CALL + PUT) OPERATIONS
# --------------------------------------------------

def get_open_synth_long_for_contract(contract):
    """
    Check if we have an open synthetic long (long CALL + short PUT)
    for this specific contract.
    """
    contract = ensure_contract_populated(contract)

    call_sid = str(contract["call_security_id"])
    put_sid = str(contract["put_security_id"])

    positions = get_positions()
    if not positions:
        return None

    call_pos = None
    put_pos = None

    for p in positions:
        sid = str(p.get("securityId"))
        if sid == call_sid:
            call_pos = p
        elif sid == put_sid:
            put_pos = p

    if not call_pos or not put_pos:
        return None

    call_net = call_pos.get("netQty", 0)
    put_net = put_pos.get("netQty", 0)

    # synthetic long: call long (>0) + put short (<0)
    if call_net > 0 and put_net < 0:
        qty = min(call_net, -put_net)
        if qty <= 0:
            return None
        return {
            "contract": contract,
            "qty": int(qty),
            "call_net": call_net,
            "put_net": put_net,
        }

    return None


def get_open_synth_long_any():
    """Find any open synthetic long across configured expiries."""
    for c in sorted_synth_contracts():
        info = get_open_synth_long_for_contract(c)
        if info:
            return info
    return None


def enter_synthetic_long(contract, qty: int):
    """
    Long synthetic future = Buy CALL + Sell PUT.
    RULE: ALWAYS execute BUY leg first, and wait for fill.
    """
    contract = ensure_contract_populated(contract)

    call_sid = contract["call_security_id"]
    put_sid = contract["put_security_id"]

    print(
        f"[ENTER SYN] {contract['name']} qty={qty} CALL={call_sid} PUT={put_sid} strike={contract.get('strike')}"
    )

    # 1) BUY CALL – ensure filled
    buy_call = place_order_with_checks("BUY", call_sid, qty, ensure_fill=True)
    if not buy_call.get("placed") or not buy_call.get("filled_completely", False):
        print("[ENTER SYN] BUY CALL failed/not filled -> NOT placing SELL PUT.")
        return {
            "entered": False,
            "reason": "buy_call_failed_or_not_filled",
            "buy_call": buy_call,
        }

    # 2) SELL PUT
    sell_put = place_order_with_checks("SELL", put_sid, qty, ensure_fill=False)

    return {
        "entered": True,
        "contract": contract["name"],
        "qty": qty,
        "buy_call": buy_call,
        "sell_put": sell_put,
    }


def exit_synthetic_long(contract, qty: int):
    """
    Exit synthetic long:
      existing = long CALL + short PUT
      exit     = BUY PUT (close short) then SELL CALL (close long)
    RULE: BUY leg first, then SELL leg.
    """
    contract = ensure_contract_populated(contract)

    call_sid = contract["call_security_id"]
    put_sid = contract["put_security_id"]

    print(
        f"[EXIT SYN] {contract['name']} qty={qty} CALL={call_sid} PUT={put_sid} strike={contract.get('strike')}"
    )

    # 1) BUY PUT – ensure filled
    buy_put = place_order_with_checks("BUY", put_sid, qty, ensure_fill=True)
    if not buy_put.get("placed") or not buy_put.get("filled_completely", False):
        print("[EXIT SYN] BUY PUT failed/not filled -> NOT placing SELL CALL.")
        return {
            "exited": False,
            "reason": "buy_put_failed_or_not_filled",
            "buy_put": buy_put,
        }

    # 2) SELL CALL
    sell_call = place_order_with_checks("SELL", call_sid, qty, ensure_fill=False)

    return {
        "exited": True,
        "contract": contract["name"],
        "qty": qty,
        "buy_put": buy_put,
        "sell_call": sell_call,
    }


def rollover_synthetic_if_needed(today: date):
    """
    If today is expiry of near-month synthetic contract and there is an
    open synthetic long, roll it to next-month.

    1) EXIT current expiry synthetic first
    2) Then ENTER new synthetic in next expiry
    (each multi-leg step itself obeys BUY-first-then-SELL)
    """
    near, next_c = get_near_and_next_contract(today)
    if near is None or next_c is None:
        return None

    if near["expiry"] != today:
        return None

    print(f"[ROLLOVER] Today is expiry of {near['name']} -> checking open synthetic long")

    open_info = get_open_synth_long_for_contract(near)
    if not open_info:
        print("[ROLLOVER] No open synthetic long in expiring contract.")
        return None

    qty = open_info["qty"]
    print(f"[ROLLOVER] Required qty={qty} from {near['name']} to {next_c['name']}")

    # 1) EXIT old synthetic first
    exit_res = exit_synthetic_long(near, qty)
    if not exit_res.get("exited"):
        print("[ROLLOVER] Exit old synthetic FAILED -> NOT entering new synthetic.")
        return {
            "rolled": False,
            "reason": "exit_old_failed",
            "exit_result": exit_res,
        }

    # 2) ENTER new synthetic after exit
    enter_res = enter_synthetic_long(next_c, qty)
    if not enter_res.get("entered"):
        print("[ROLLOVER] Enter new synthetic FAILED AFTER exit.")
        return {
            "rolled": False,
            "reason": "enter_new_failed_after_exit",
            "exit_result": exit_res,
            "enter_result": enter_res,
        }

    return {
        "rolled": True,
        "qty": qty,
        "from_contract": near["name"],
        "to_contract": next_c["name"],
        "exit_result": exit_res,
        "enter_result": enter_res,
    }


# --------------------------------------------------
# FLASK ROUTES
# --------------------------------------------------

@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    """
    TradingView webhook endpoint.

    Expected JSON body, e.g.:
      {
        "signal": "{{strategy.order.action}}",  // BUY / SELL
        "underlying": "NIFTY",
        "qty": 75
      }
    """
    data = request.get_json() or {}
    print("[TV] Received payload:", data)

    raw_signal = str(data.get("signal", "")).upper()
    qty = int(data.get("qty", 75))
    underlying = str(data.get("underlying", "NIFTY")).upper()
    today = date.today()

    if underlying != "NIFTY":
        return (
            jsonify({"status": "error", "reason": f"Unsupported underlying: {underlying}"}),
            400,
        )

    # On ANY signal day, first check if rollover is needed
    rollover_info = rollover_synthetic_if_needed(today)

    # ---- NEW ENTRY: long synthetic ----
    if raw_signal == "BUY":
        contract = get_contract_for_new_long(today)
        if not contract:
            return (
                jsonify(
                    {
                        "status": "error",
                        "reason": "No NIFTY synthetic contracts available (metadata not loaded?)",
                    }
                ),
                500,
            )

        enter_res = enter_synthetic_long(contract, qty)
        ok = enter_res.get("entered", False)

        return jsonify(
            {
                "status": "ok" if ok else "failed",
                "mode": "LIVE" if LIVE else "PAPER",
                "action": "ENTER_SYNTH_LONG",
                "contract": contract["name"],
                "qty": qty,
                "rollover": rollover_info,
                "result": enter_res,
            }
        ), 200

    # ---- EXIT synthetic long ----
    elif raw_signal in ("SELL", "EXIT"):
        open_info = get_open_synth_long_any()
        if not open_info:
            return jsonify(
                {
                    "status": "ignored",
                    "reason": "No open NIFTY synthetic long to close",
                    "rollover": rollover_info,
                }
            )

        contract = open_info["contract"]
        pos_qty = open_info["qty"]

        exit_res = exit_synthetic_long(contract, pos_qty)
        ok = exit_res.get("exited", False)

        return jsonify(
            {
                "status": "ok" if ok else "failed",
                "mode": "LIVE" if LIVE else "PAPER",
                "action": "EXIT_SYNTH_LONG",
                "contract": contract["name"],
                "closed_qty": pos_qty,
                "rollover": rollover_info,
                "result": exit_res,
            }
        ), 200

    else:
        return (
            jsonify({"status": "ignored", "reason": f"invalid signal: {raw_signal}"}),
            400,
        )


@app.route("/dhan-postback", methods=["POST"])
def dhan_postback():
    """
    Dhan Postback (order update) webhook receiver.

    Configure this URL in Dhan token generation:
      https://<your-app>.onrender.com/dhan-postback
    """
    payload = request.get_json() or {}
    print("[DHAN POSTBACK]", payload)

    order_id = str(payload.get("orderId", "") or "")
    if order_id:
        ORDER_STATUS_CACHE[order_id] = payload

    # Dhan just expects a 200 with any body
    return jsonify({"status": "ok"})


@app.route("/health/dhan", methods=["GET"])
def health_dhan():
    """
    Quick health check to verify that DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID
    are valid and Dhan API is reachable.
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "access-token": DHAN_ACCESS_TOKEN,
            "client-id": DHAN_CLIENT_ID,
        }

        resp = requests.get(f"{DHAN_BASE_URL}/profile", headers=headers, timeout=3)

        try:
            body = resp.json()
        except Exception:
            body = resp.text

        return jsonify({
            "ok": resp.status_code == 200,
            "status_code": resp.status_code,
            "response": body,
            "using_token": True if DHAN_ACCESS_TOKEN else False,
            "using_client_id": True if DHAN_CLIENT_ID else False
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "using_token": True if DHAN_ACCESS_TOKEN else False,
            "using_client_id": True if DHAN_CLIENT_ID else False
        }), 500


@app.route("/")
def home():
    return "Dhan webhook server is running – synthetic NIFTY (CALL+PUT) bot (auto ATM, monthly expiry)."


if __name__ == "__main__":
    app.run()







