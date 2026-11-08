"""
Price Target Tracking - "I bought AAPL at $185, ping me at $210"
"""
import json, os, time, threading, logging
from typing import List, Dict
from datetime import datetime, timezone

from config import DATA_DIR, TARGETS_FILE
import data_sources as ds
import alerts

log = logging.getLogger("jarvis.targets")

def _load() -> List[Dict]:
    try:
        with open(TARGETS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _save(targets: List[Dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TARGETS_FILE, "w") as f:
        json.dump(targets, f, indent=2)

def add_target(asset: str, asset_type: str, entry_price: float,
               target_price: float, direction: str = "above") -> Dict:
    targets = _load()
    entry = {
        "id": f"t_{int(time.time()*1000)}",
        "asset": asset,
        "asset_type": asset_type,
        "entry_price": entry_price,
        "target_price": target_price,
        "direction": direction,
        "active": True,
        "created": datetime.now(timezone.utc).isoformat(),
        "hit": False,
        "hit_price": None,
    }
    targets.append(entry)
    _save(targets)
    log.info(f"Target added: {asset} {direction} ${target_price}")
    return entry

def remove_target(target_id: str) -> bool:
    targets = _load()
    before = len(targets)
    targets = [t for t in targets if t["id"] != target_id]
    _save(targets)
    return len(targets) < before

def list_targets(active_only: bool = False) -> List[Dict]:
    targets = _load()
    if active_only:
        targets = [t for t in targets if t["active"]]
    return targets

def check_all_targets() -> List[Dict]:
    targets = _load()
    active = [t for t in targets if t["active"] and not t["hit"]]
    if not active:
        return []
    hits = []
    for t in active:
        try:
            if t["asset_type"] == "stock":
                quote = ds.fetch_stock_quote(t["asset"].upper())
                price = quote.get("price", 0)
            else:
                prices = ds.fetch_crypto_prices([t["asset"].lower()])
                price = float(prices.get(t["asset"].lower(), {}).get("usd", 0))
            if price == 0:
                continue
            hit = False
            if t["direction"] == "above" and price >= t["target_price"]:
                hit = True
            elif t["direction"] == "below" and price <= t["target_price"]:
                hit = True
            if hit:
                t["hit"] = True
                t["hit_price"] = price
                t["hit_time"] = datetime.now(timezone.utc).isoformat()
                alerts.alert_price_target(t["asset"], t["target_price"],
                                          price, t["direction"])
                hits.append(t)
                log.info(f"TARGET HIT: {t['asset']} @ ${price}")
        except Exception as e:
            log.error(f"Target check error for {t['asset']}: {e}")
    if hits:
        _save(targets)
    return hits

def start_target_checker(interval_sec: int = 30):
    def _loop():
        while True:
            try:
                check_all_targets()
            except Exception as e:
                log.error(f"Target check loop error: {e}")
            time.sleep(interval_sec)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info(f"Price target checker started (every {interval_sec}s)")
