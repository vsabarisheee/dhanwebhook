from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

DHAN_API_KEY = os.getenv("DHAN_API_KEY")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")
DHAN_BASE_URL = "https://api.dhan.co"

def place_order(signal, symbol, qty):
    transaction_type = "BUY" if signal == "BUY" else "SELL"

    payload = {
        "transactionType": transaction_type,
        "exchangeSegment": "NSE_FNO",
        "productType": "INTRADAY",
        "orderType": "MARKET",
        "securityId": symbol,
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
    data = request.get_json()
    signal = data.get("signal")
    symbol = data.get("symbol")
    qty    = data.get("qty", 75)
    status, res = place_order(signal, symbol, qty)
    return jsonify({"status": status, "dhan_response": res})

@app.route("/")
def home():
    return "Dhan webhook server is running!"

if __name__ == "__main__":
    app.run()
