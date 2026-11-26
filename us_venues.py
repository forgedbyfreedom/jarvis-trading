"""
US-legal perpetual futures venues for CopyBot: Kalshi perps (primary) and Coinbase US
perp-style futures (CDE, secondary).

Two layers:
  * Public market data (no keys): which coins are listed, contract size, price per contract,
    minimum quantity. Used by the shadow book every cycle.
  * Signed account clients (keys from .env.local, entered by Bryan on the PC): balance,
    positions, orders, cancel-all. Only called when live trading is armed, or for the
    read-only "Test connection" button.

Keys (put these in C:\\jarvis-trading\\.env.local yourself — never paste them into chat):
  KALSHI_API_KEY_ID=...            Kalshi API key id
  KALSHI_PRIVATE_KEY_PATH=...      path to the PEM private key Kalshi gave you
  COINBASE_CDP_KEY_NAME=...        organizations/.../apiKeys/...  (CDP key, Trade permission only)
  COINBASE_CDP_PRIVATE_KEY_PATH=.. path to the PEM file for that key
Create both keys WITHOUT withdraw / transfer permission.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any

import requests

log = logging.getLogger("jarvis.venues")

UA = {"User-Agent": "FBF-Jarvis/4 (personal)", "Accept": "application/json"}
KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"
CB_BASE = "https://api.coinbase.com"

# Taker fees (market-style orders). Kalshi crypto perps: 0.04% tier 0 per its CFTC fee filing;
# Coinbase US perp-style futures: advertised "as low as 0.02%" with a $0.15 minimum — we charge
# 0.03% to stay conservative. Change here if your account tier differs.
FEES = {
    "kalshi":   {"taker": 0.0004, "min_fee": 0.0,  "slippage_bps": 3.0, "name": "Kalshi perps"},
    "coinbase": {"taker": 0.0003, "min_fee": 0.15, "slippage_bps": 3.0, "name": "Coinbase US perps"},
}
VENUE_ORDER = ("kalshi", "coinbase")   # Kalshi first: 1-contract minimum is ~$2-7 for majors

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
CACHE_S = 120


def norm_coin(c: str) -> str:
    """Hyperliquid's kPEPE / kSHIB style names -> PEPE / SHIB."""
    c = c.strip()
    if len(c) > 1 and c[0] == "k" and c[1:].isupper():
        return c[1:]
    return c.upper()


def _get(url: str, params: dict | None = None, timeout: int = 15) -> Any:
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _cached(key: str, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


# ─────────────────────────────────────────────── public market data
def kalshi_markets() -> dict[str, dict]:
    """coin -> {ticker, contract_size, px_contract, bid, ask, min_qty, tick, open}"""
    def load():
        out, cursor = {}, None
        for _ in range(10):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get(f"{KALSHI_BASE}/margin/markets", params)
            for m in d.get("markets", []):
                if m.get("asset_class") != "Crypto" or m.get("status") != "active":
                    continue
                coin = norm_coin((m.get("title") or "").split()[-1])
                px = float(m.get("price") or 0)
                if not coin or px <= 0:
                    continue
                out[coin] = {
                    "venue": "kalshi", "symbol": m["ticker"], "contract_size": float(m["contract_size"]),
                    "px_contract": px, "bid": float(m.get("bid") or px), "ask": float(m.get("ask") or px),
                    "min_qty": float(m.get("minimum_quantity") or 1), "tick": float(m.get("tick_size") or 0.0001),
                    "open": bool((m.get("schedule") or {}).get("is_open", True)),
                }
            cursor = d.get("cursor")
            if not cursor:
                break
        return out
    return _cached("kalshi", load)


def coinbase_markets() -> dict[str, dict]:
    """coin -> {product_id, contract_size, px_contract, ...} for US perp-style futures (-CDE)."""
    def load():
        d = _get(f"{CB_BASE}/api/v3/brokerage/market/products", {"product_type": "FUTURE", "limit": 500})
        out = {}
        for p in d.get("products", []):
            f = p.get("future_product_details") or {}
            if not p.get("product_id", "").endswith("-CDE"):
                continue
            if f.get("futures_asset_type") != "FUTURES_ASSET_TYPE_CRYPTO" or "Perpetual" not in (f.get("display_name") or ""):
                continue
            if p.get("trading_disabled") or p.get("is_disabled"):
                continue
            coin = norm_coin(f.get("contract_root_unit") or "")
            size = float(f.get("contract_size") or 0)
            px = float(p.get("price") or 0)
            if not coin or size <= 0 or px <= 0:
                continue
            out[coin] = {
                "venue": "coinbase", "symbol": p["product_id"], "contract_size": size,
                "px_contract": px * size, "bid": px * size, "ask": px * size,
                "min_qty": float(p.get("base_min_size") or 1), "tick": float(p.get("quote_increment") or 0.01),
                "open": True, "funding_rate": float(f.get("funding_rate") or 0),
            }
        return out
    return _cached("coinbase", load)


def markets() -> dict[str, dict[str, dict]]:
    res = {}
    for v, fn in (("kalshi", kalshi_markets), ("coinbase", coinbase_markets)):
        try:
            res[v] = fn()
        except Exception as e:
            log.warning("%s market data: %s", v, e)
            res[v] = {}
    return res


def trade_fee(venue: str, notional: float) -> float:
    f = FEES[venue]
    return max(abs(notional) * f["taker"], f["min_fee"]) if notional else 0.0


# ─────────────────────────────────────────────── secrets / keys
def _secret(name: str) -> str:
    try:
        import hub
        return hub._secret(name, "") or ""
    except Exception:
        return os.environ.get(name, "")


def _read_key_file(path: str) -> bytes | None:
    if not path:
        return None
    path = os.path.expanduser(path.strip().strip('"'))
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def keys_present(venue: str) -> bool:
    if venue == "kalshi":
        return bool(_secret("KALSHI_API_KEY_ID") and _read_key_file(_secret("KALSHI_PRIVATE_KEY_PATH")))
    if venue == "coinbase":
        return bool(_secret("COINBASE_CDP_KEY_NAME") and _read_key_file(_secret("COINBASE_CDP_PRIVATE_KEY_PATH")))
    return False


# ─────────────────────────────────────────────── Kalshi signed client
class Kalshi:
    venue = "kalshi"

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        self.key_id = _secret("KALSHI_API_KEY_ID")
        pem = _read_key_file(_secret("KALSHI_PRIVATE_KEY_PATH"))
        if not (self.key_id and pem):
            raise RuntimeError("Kalshi keys not set in .env.local")
        self.pk = serialization.load_pem_private_key(pem, password=None)

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, ed25519
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + KALSHI_PATH_PREFIX + path.split("?")[0]).encode()
        if isinstance(self.pk, ed25519.Ed25519PrivateKey):
            sig = self.pk.sign(msg)
        else:
            sig = self.pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json", **UA}

    def _req(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        r = requests.request(method, KALSHI_BASE + path, headers=self._sign(method, path),
                             params=params, data=json.dumps(body) if body is not None else None, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def balance(self) -> dict:
        d = self._req("GET", "/margin/balance")
        prim = next((b for b in d.get("subaccount_balances", []) if b.get("subaccount") == 0),
                    (d.get("subaccount_balances") or [{}])[0])
        return {"equity": float(prim.get("account_equity") or 0), "available": float(prim.get("available_balance") or 0),
                "raw_settled": d.get("settled_funds")}

    def positions(self) -> dict[str, float]:
        d = self._req("GET", "/margin/positions")
        out = {}
        for p in d.get("positions", []):
            if p.get("subaccount", 0) == 0:
                out[p["market_ticker"]] = out.get(p["market_ticker"], 0.0) + float(p.get("position") or 0)
        return out

    def market_order(self, mk: dict, contracts: float, reduce_only: bool = False) -> dict:
        """IOC limit 0.5% through the book — fills like a market order but can't run away."""
        buy = contracts > 0
        ref = mk["ask"] if buy else mk["bid"]
        px = ref * (1.005 if buy else 0.995)
        tick = mk["tick"] or 0.0001
        px = round(round(px / tick) * tick, 4)
        body = {"ticker": mk["symbol"], "client_order_id": str(uuid.uuid4()), "side": "bid" if buy else "ask",
                "count": f"{abs(contracts):.2f}", "price": f"{px:.4f}", "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross"}
        if reduce_only:
            body["reduce_only"] = True
        d = self._req("POST", "/margin/orders", body)
        o = d.get("order", d)
        return {"order_id": o.get("order_id"), "filled": float(o.get("fill_count") or 0),
                "avg_px": float(o.get("average_fill_price") or 0), "fee": float(o.get("average_fee_paid") or 0)
                * float(o.get("fill_count") or 0)}

    def cancel_all(self) -> None:
        self._req("DELETE", "/margin/orders")


# ─────────────────────────────────────────────── Coinbase (CDP JWT) signed client
class Coinbase:
    venue = "coinbase"

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        self.key_name = _secret("COINBASE_CDP_KEY_NAME")
        pem = _read_key_file(_secret("COINBASE_CDP_PRIVATE_KEY_PATH"))
        if not (self.key_name and pem):
            raise RuntimeError("Coinbase CDP keys not set in .env.local")
        self.pk = serialization.load_pem_private_key(pem, password=None)

    def _jwt(self, method: str, path: str) -> str:
        import jwt  # PyJWT
        from cryptography.hazmat.primitives.asymmetric import ed25519
        now = int(time.time())
        alg = "EdDSA" if isinstance(self.pk, ed25519.Ed25519PrivateKey) else "ES256"
        payload = {"sub": self.key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
                   "uri": f"{method.upper()} api.coinbase.com{path.split('?')[0]}"}
        return jwt.encode(payload, self.pk, algorithm=alg, headers={"kid": self.key_name, "nonce": secrets.token_hex()})

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        h = {"Authorization": "Bearer " + self._jwt(method, path), "Content-Type": "application/json", **UA}
        r = requests.request(method, CB_BASE + path, headers=h,
                             data=json.dumps(body) if body is not None else None, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Coinbase {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def balance(self) -> dict:
        d = self._req("GET", "/api/v3/brokerage/cfm/balance_summary").get("balance_summary", {})
        val = lambda k: float((d.get(k) or {}).get("value") or 0)
        return {"equity": val("total_usd_balance"), "available": val("futures_buying_power")}

    def positions(self) -> dict[str, float]:
        d = self._req("GET", "/api/v3/brokerage/cfm/positions")
        out = {}
        for p in d.get("positions", []):
            n = float(p.get("number_of_contracts") or 0)
            if str(p.get("side", "")).upper() == "SHORT":
                n = -abs(n)
            out[p["product_id"]] = out.get(p["product_id"], 0.0) + n
        return out

    def market_order(self, mk: dict, contracts: float, reduce_only: bool = False) -> dict:
        body = {"client_order_id": str(uuid.uuid4()), "product_id": mk["symbol"],
                "side": "BUY" if contracts > 0 else "SELL",
                "order_configuration": {"market_market_ioc": {"base_size": str(int(abs(contracts)))}}}
        d = self._req("POST", "/api/v3/brokerage/orders", body)
        if not d.get("success", True):
            raise RuntimeError(f"Coinbase rejected order: {json.dumps(d.get('error_response'))[:300]}")
        oid = (d.get("success_response") or {}).get("order_id")
        return {"order_id": oid, "filled": abs(contracts), "avg_px": 0.0, "fee": 0.0}

    def cancel_all(self) -> None:
        d = self._req("GET", "/api/v3/brokerage/orders/historical/batch?order_status=OPEN&product_type=FUTURE")
        ids = [o["order_id"] for o in d.get("orders", [])]
        if ids:
            self._req("POST", "/api/v3/brokerage/orders/batch_cancel", {"order_ids": ids})


def client(venue: str):
    return Kalshi() if venue == "kalshi" else Coinbase()


def test_connection(venue: str) -> dict:
    """Read-only: balance + positions. Never places an order."""
    if not keys_present(venue):
        return {"ok": False, "venue": venue, "error": "Keys not found in .env.local"}
    try:
        c = client(venue)
        return {"ok": True, "venue": venue, "balance": c.balance(), "positions": c.positions()}
    except Exception as e:
        return {"ok": False, "venue": venue, "error": str(e)[:400]}
