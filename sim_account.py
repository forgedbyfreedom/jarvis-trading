"""
Dry-run account ledger for the auto-trader.

Tracks what the simulated account would really hold after every fee:
starting capital, realized P&L before and after costs, each fee category,
and an estimated tax set-aside. Also re-scores the old fee-free paper
history so you can see what those trades would have cost.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import config as C
import fees

ACCOUNT_FILE = os.path.join(C.DATA_DIR, "sim_account.json")
ARCHIVE_DIR = os.path.join(C.DATA_DIR, "archive")
_lock = threading.RLock()


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _fresh() -> dict:
    return {
        "mode": C.TRADING_MODE,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "starting_capital": C.SIM_STARTING_CAPITAL,
        "realized_gross": 0.0,
        "realized_net": 0.0,
        "fees": {},
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "last_exit": {},
        "skipped": {},
    }


def load() -> dict:
    acct = _read(ACCOUNT_FILE, None)
    if acct is None:
        acct = start_fresh(archive_old=True)
    return acct


def start_fresh(archive_old: bool = True) -> dict:
    """Begin a new dry run. Old fee-free history is archived, never deleted."""
    with _lock:
        if archive_old:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            for name in ("trade_history.json", "positions.json", "daily_pnl.json", "sim_account.json"):
                src = os.path.join(C.DATA_DIR, name)
                if os.path.exists(src) and os.path.getsize(src) > 2:
                    shutil.move(src, os.path.join(ARCHIVE_DIR, f"{stamp}_{name}"))
        acct = _fresh()
        _write(ACCOUNT_FILE, acct)
        return acct


def equity(open_positions: list | None = None, marks: dict | None = None) -> float:
    acct = load()
    eq = acct["starting_capital"] + acct["realized_net"]
    for p in open_positions or []:
        mark = (marks or {}).get(p["id"])
        if mark:
            d = 1 if p["side"] == "buy" else -1
            eq += (mark - p["entry_price"]) * p["qty"] * d - p.get("entry_costs_total", 0)
    return round(eq, 2)


def note_skip(reason: str) -> None:
    with _lock:
        acct = load()
        acct["skipped"][reason] = acct["skipped"].get(reason, 0) + 1
        _write(ACCOUNT_FILE, acct)


def record_close(asset_key: str, gross: float, cost_breakdown: dict) -> float:
    """Book a closed trade; returns net P&L."""
    with _lock:
        acct = load()
        total_cost = sum(cost_breakdown.values())
        net = gross - total_cost
        acct["realized_gross"] = round(acct["realized_gross"] + gross, 4)
        acct["realized_net"] = round(acct["realized_net"] + net, 4)
        for k, v in cost_breakdown.items():
            acct["fees"][k] = round(acct["fees"].get(k, 0) + v, 4)
        acct["closed_trades"] += 1
        acct["wins" if net > 0 else "losses"] += 1
        acct["last_exit"][asset_key] = datetime.now().isoformat(timespec="seconds")
        _write(ACCOUNT_FILE, acct)
        return round(net, 4)


def last_exit(asset_key: str):
    ts = load()["last_exit"].get(asset_key)
    return datetime.fromisoformat(ts) if ts else None


def summary(open_positions: list | None = None, marks: dict | None = None) -> dict:
    acct = load()
    fees_total = round(sum(acct["fees"].values()), 2)
    n = acct["closed_trades"]
    return {
        "mode": C.TRADING_MODE,
        "started_at": acct["started_at"],
        "starting_capital": acct["starting_capital"],
        "equity": equity(open_positions, marks),
        "realized_gross": round(acct["realized_gross"], 2),
        "fees_total": fees_total,
        "fees": {k: round(v, 2) for k, v in sorted(acct["fees"].items(), key=lambda kv: -kv[1])},
        "realized_net": round(acct["realized_net"], 2),
        "est_tax": fees.estimated_tax(acct["realized_net"]),
        "after_tax": round(acct["realized_net"] - fees.estimated_tax(acct["realized_net"]), 2),
        "closed_trades": n,
        "win_rate": round(100 * acct["wins"] / n, 1) if n else 0,
        "skipped": acct["skipped"],
        "crypto_venue": C.CRYPTO_VENUES[C.CRYPTO_VENUE]["name"],
    }


# ───────────────────────────── re-score the old, fee-free paper run
def rescore_legacy() -> dict | None:
    """Apply today's cost model to the most recent archived fee-free history."""
    if not os.path.isdir(ARCHIVE_DIR):
        return None
    files = sorted(f for f in os.listdir(ARCHIVE_DIR) if f.endswith("_trade_history.json"))
    if not files:
        return None
    hist = _read(os.path.join(ARCHIVE_DIR, files[0]), [])  # oldest = original pre-fee run
    closed = [t for t in hist if t.get("status") == "closed" and t.get("qty")]
    if not closed or any("costs" in t for t in closed):
        return None
    et = ZoneInfo("America/New_York")
    gross = cost = 0.0
    by_cat: Counter = Counter()
    after_hours = 0
    regular_gross = 0.0
    for t in closed:
        qty, exit_px, pnl = float(t["qty"]), float(t["price"]), float(t.get("pnl", 0))
        # closing side "sell" = closing a long; "buy" = covering a short
        is_long = t["side"] == "sell"
        entry_px = exit_px - pnl / qty if is_long else exit_px + pnl / qty
        open_side, close_side = ("buy", "sell") if is_long else ("sell", "buy")
        for side, px in ((open_side, entry_px), (close_side, exit_px)):
            f = fees.fill(t["asset_type"], side, px, qty)
            for k, v in f["costs"].items():
                by_cat[k] += v
                cost += v
        gross += pnl
        if t["asset_type"] == "stock":
            ts = datetime.fromisoformat(t["timestamp"]).replace(tzinfo=et)
            if not fees.us_market_open(ts):
                after_hours += 1
            else:
                regular_gross += pnl
        else:
            regular_gross += pnl
    return {
        "source": files[0],
        "closed_trades": len(closed),
        "gross_pnl": round(gross, 2),
        "costs": round(cost, 2),
        "costs_by_type": {k: round(v, 2) for k, v in by_cat.most_common()},
        "net_pnl": round(gross - cost, 2),
        "stock_exits_outside_market_hours": after_hours,
        "gross_excluding_closed_market_exits": round(regular_gross, 2),
    }
