from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# --------- CONFIG ---------
DHAN_API_KEY = os.getenv("DHAN_API_KEY")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")
DHAN_BASE_URL = "https://api.dhan.co"

# PAPER / LIVE switch
LIVE = False  # set to True only after you are happy with tests


def place_order(signal, symbol, qty):
    transaction_type = "BUY" if signal == "BUY" else "SELL"

    payload = {
        "transactionType": transaction_type,
        "exchangeSegment": "NSE_FNO",
        "productType": "INTRADAY",
        "orderType": "MARKET",
        "securityId": symbol,     # here 'symbol' is actually Dhan securityId
        "quantity": int(qty),
        "validity": "DAY"
    }

    headers = {
        "Content-Type": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_API_KEY
    }

    response = requests.post(f"{DHAN_BASE_URL}/orders", json=payload, headers=headers)
    return response.status_code, response.text


@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json() or {}
    print("Received payload:", data)

    signal = str(data.get("signal", "")).upper()
    symbol = data.get("symbol")   # this should be Dhan securityId
    qty    = data.get("qty", 75)

    if signal not in ("BUY", "SELL"):
        return jsonify({"status": "ignored", "reason": f"invalid signal: {signal}"}), 400

    if not symbol:
        return jsonify({"status": "error", "reason": "symbol (securityId) missing"}), 400

    # PAPER MODE
    if not LIVE:
        print(f"[PAPER] Would place {signal} {qty} on {symbol}")
        return jsonify({
            "status": "paper",
            "signal": signal,
            "symbol": symbol,
            "qty": qty
        }), 200

    # LIVE MODE
    status, res = place_order(signal, symbol, qty)
    return jsonify({"status": status, "dhan_response": res}), status


@app.route("/")
def home():
    return "Dhan webhook server is running!"


if __name__ == "__main__":
    app.run()
