"""
Trade Execution - Stocks (Alpaca) + Crypto (Coinbase).
HARD RULE: No trade without user_confirmed == True.
"""
import hashlib, hmac, time, json, os, requests, logging
from datetime import datetime, timezone
from typing import List, Dict

from config import (
    ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL,
    COINBASE_API_KEY, COINBASE_API_SECRET, COINBASE_PASSPHRASE,
    REQUIRE_CONFIRM, DATA_DIR, AUDIT_FILE,
)
import alerts

log = logging.getLogger("jarvis.exec")

def _load_audit() -> List[Dict]:
    try:
        with open(AUDIT_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _save_audit(entries: List[Dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AUDIT_FILE, "w") as f:
        json.dump(entries[-500:], f, indent=2)

def _audit(action: str, details: dict):
    entries = _load_audit()
    entries.append({"ts": datetime.now(timezone.utc).isoformat(),
                    "action": action, "details": details})
    _save_audit(entries)
    log.info(f"AUDIT: {action} {details}")

def get_audit_log() -> List[Dict]:
    return _load_audit()

# STOCKS - ALPACA
def _alpaca_headers(method: str, path: str) -> dict:
    ts  = str(int(time.time()))
    msg = f"{ts}{method}{path}"
    sig = hmac.new(ALPACA_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
        "APCA-TIMESTAMP": ts, "APCA-SIGNATURE": sig,
    }

def execute_stock_trade(symbol: str, side: str, qty: float,
                        user_confirmed: bool, price: float = 0) -> dict:
    if REQUIRE_CONFIRM and not user_confirmed:
        _audit("STOCK_REJECTED", {"reason": "user did not confirm", "symbol": symbol})
        return {"status": "rejected", "reason": "User confirmation required"}
    if not ALPACA_API_KEY:
        _audit("STOCK_PAPER", {"symbol": symbol, "side": side, "qty": qty})
        alerts.alert_trade_executed(symbol, side, qty, price, "paper")
        return {"status": "paper", "msg": f"[PAPER] {side.upper()} {qty} {symbol}"}
    path = "/v2/orders"
    headers = _alpaca_headers("POST", path)
    payload = {"symbol": symbol, "qty": str(qty), "side": side,
               "type": "market", "time_in_force": "day"}
    try:
        r = requests.post(f"{ALPACA_BASE_URL}{path}", json=payload,
                          headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        _audit("STOCK_EXECUTED", {"symbol": symbol, "side": side,
                                   "qty": qty, "order_id": data.get("id")})
        alerts.alert_trade_executed(symbol, side, qty, price, "filled")
        return {"status": "filled", "order": data}
    except Exception as e:
        _audit("STOCK_ERROR", {"symbol": symbol, "side": side, "error": str(e)})
        return {"status": "error", "error": str(e)}

def get_stock_portfolio() -> dict:
    if not ALPACA_API_KEY:
        return {"cash": 0, "positions": [], "mode": "paper"}
    try:
        path = "/v2/account"
        headers = _alpaca_headers("GET", path)
        acct = requests.get(f"{ALPACA_BASE_URL}{path}", headers=headers, timeout=10).json()
        pos_path = "/v2/positions"
        pos_h = _alpaca_headers("GET", pos_path)
        positions = requests.get(f"{ALPACA_BASE_URL}{pos_path}",
                                 headers=pos_h, timeout=10).json()
        return {"cash": float(acct.get("cash", 0)),
                "portfolio_value": float(acct.get("equity", 0)),
                "positions": positions, "mode": acct.get("status", "paper")}
    except Exception as e:
        return {"cash": 0, "positions": [], "error": str(e)}

# CRYPTO - COINBASE
def _coinbase_headers(method: str, path: str) -> dict:
    ts  = str(int(time.time()))
    msg = f"{ts}{method}{path}"
    sig = hmac.new(COINBASE_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "CB-ACCESS-KEY": COINBASE_API_KEY,
        "CB-ACCESS-SIGN": sig,
        "CB-ACCESS-TIMESTAMP": ts,
        "CB-ACCESS-PASSPHRASE": COINBASE_PASSPHRASE,
        "Content-Type": "application/json",
    }

def execute_crypto_trade(product_id: str, side: str, qty: float,
                         user_confirmed: bool, price: float = 0) -> dict:
    if REQUIRE_CONFIRM and not user_confirmed:
        _audit("CRYPTO_REJECTED", {"reason": "user did not confirm", "product": product_id})
        return {"status": "rejected", "reason": "User confirmation required"}
    if not COINBASE_API_KEY:
        _audit("CRYPTO_PAPER", {"product": product_id, "side": side, "qty": qty})
        alerts.alert_trade_executed(product_id, side, qty, price, "paper")
        return {"status": "paper", "msg": f"[PAPER] {side.upper()} {qty} {product_id}"}
    path = "/api/v3/brokerage/orders"
    headers = _coinbase_headers("POST", path)
    payload = {
        "client_order_id": f"jarvis-{int(time.time()*1000)}",
        "order_config": {"market_market_ioc_config": {
            "base_size": str(qty), "base_increment": "0.000001"}},
        "side": side.upper(), "product_id": product_id,
    }
    try:
        r = requests.post(f"https://api.exchange.coinbase.com{path}",
                          json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        _audit("CRYPTO_EXECUTED", {"product": product_id, "side": side, "qty": qty})
        alerts.alert_trade_executed(product_id, side, qty, price, "filled")
        return {"status": "filled", "order": data}
    except Exception as e:
        _audit("CRYPTO_ERROR", {"product": product_id, "side": side, "error": str(e)})
        return {"status": "error", "error": str(e)}

def get_crypto_balances() -> dict:
    if not COINBASE_API_KEY:
        return {"balances": [], "mode": "paper"}
    try:
        path = "/api/v3/brokerage/accounts"
        headers = _coinbase_headers("GET", path)
        r = requests.get(f"https://api.exchange.coinbase.com{path}",
                         headers=headers, timeout=10)
        return {"balances": r.json(), "mode": "live"}
    except Exception as e:
        return {"balances": [], "error": str(e)}

def get_funding_links() -> dict:
    return {
        "cash_app": "https://cash.app/",
        "bank": "https://yourbank.com/login",
        "note": "Hard Rule: I never pull money. You click, you authorize, it moves.",
    }
