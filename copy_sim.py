"""
CopyBot dry run inside Jarvis: mirrors the top Hyperliquid wallets on a
simulated $10,000 account, using the same wallet ranking and position sizing
as github.com/forgedbyfreedom/copybot, and charges what Hyperliquid would:
taker fee on every rebalance, slippage, and hourly funding on open perps.

No keys, no orders: it only reads Hyperliquid's public API.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import config as C
import copy_live
import fees
from copybot_lib import hl_source as hl
from copybot_lib.core import post_json_with_retry

log = logging.getLogger("jarvis.copy")

STATE_FILE = os.path.join(C.DATA_DIR, "copy_sim.json")
INTERVAL_S = 30 * 60   # same cadence as the GitHub Actions copybot

# Same settings as copybot/config.yaml (crypto section)
RANKING = {
    "top_n": 5, "rerank_hours": 24, "window": "month", "min_account_value": 250000,
    "min_window_volume": 1000000, "max_turnover": 100, "require_positive_alltime": True,
    "require_positive_week": True, "max_window_roi": 10.0, "candidates_to_inspect": 40,
    "exclude_addresses": [], "always_include_addresses": [],
}
SIZING = {
    "capital_fraction": 0.5, "max_gross_leverage": 3.0, "max_coin_fraction": 0.35,
    "max_coins": 15, "min_trade_usd": 15, "rebalance_band": 0.15,
}
MAX_DAILY_LOSS = 0.08
RECORD_VERSION = 2          # v2 = clean record with downtime tracking (v1 archived, not deleted)
GAP_S = 2 * INTERVAL_S      # a cycle later than this means the bot was offline

_lock = threading.RLock()
_thread = None
_status = {"last_run": None, "last_error": None, "running_cycle": False}


def _now():
    return datetime.now(timezone.utc)


def _fresh() -> dict:
    return {
        "version": RECORD_VERSION,
        "last_cycle_ts": None,
        "gaps": [],                          # [{from, to, hours}] periods the bot was offline
        "btc_start": None,                   # BTC mid at the start, for the buy-and-hold comparison
        "peak": C.SIM_STARTING_CAPITAL, "max_dd": 0.0, "daily": {},
        "started_at": _now().isoformat(timespec="seconds"),
        "starting_capital": C.SIM_STARTING_CAPITAL,
        "cash": C.SIM_STARTING_CAPITAL,      # realized balance (after all costs)
        "positions": {},                     # coin -> {szi, entry}
        "costs": {"trading_fee": 0.0, "slippage": 0.0, "funding": 0.0},
        "realized_gross": 0.0,
        "trades": [],
        "hl": {},                            # wallet selection cache (copybot format)
        "last_funding_ts": None,
        "day": {"date": None, "start_equity": None, "halted": False},
        "equity_curve": [],
    }


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return _fresh()
    if st.get("version") != RECORD_VERSION:
        # Start the clean v2 record; keep the old one on disk.
        arch = STATE_FILE.replace(".json", f"_v1_{int(time.time())}.json")
        os.replace(STATE_FILE, arch)
        log.warning("CopyBot record archived to %s; fresh v2 record started", arch)
        fresh = _fresh()
        _save(fresh)
        return fresh
    return st


def _save(st: dict) -> None:
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def _unrealized(st: dict, mids: dict) -> float:
    return sum(p["szi"] * (mids.get(c, p["entry"]) - p["entry"]) for c, p in st["positions"].items())


def _equity(st: dict, mids: dict) -> float:
    return st["cash"] + _unrealized(st, mids)


def _apply_fill(st: dict, coin: str, delta_sz: float, mid: float) -> dict:
    """Trade delta_sz (signed) at mid +/- slippage; book realized P&L on any reduction."""
    slip = mid * C.HL_SLIPPAGE_BPS / 10_000
    px = mid + slip if delta_sz > 0 else mid - slip
    notional = abs(delta_sz) * px
    fee = fees.hyperliquid_trade_fee(notional)
    pos = st["positions"].get(coin, {"szi": 0.0, "entry": px})
    old = pos["szi"]
    new = old + delta_sz
    realized = 0.0
    if old != 0 and (old > 0) != (delta_sz > 0):          # reducing or flipping
        closed = min(abs(delta_sz), abs(old))
        realized = closed * (px - pos["entry"]) * (1 if old > 0 else -1)
    if new == 0 or abs(new) < 1e-12:
        st["positions"].pop(coin, None)
    else:
        if old == 0 or (old > 0) != (new > 0):             # opened or flipped
            entry = px
        elif abs(new) > abs(old):                          # added: average in
            entry = (pos["entry"] * abs(old) + px * abs(delta_sz)) / abs(new)
        else:
            entry = pos["entry"]
        st["positions"][coin] = {"szi": new, "entry": entry}
    st["cash"] += realized - fee
    st["realized_gross"] += realized
    st["costs"]["trading_fee"] += fee
    st["costs"]["slippage"] += abs(delta_sz) * slip   # already inside px; tracked for display
    return {"coin": coin, "side": "BUY" if delta_sz > 0 else "SELL", "sz": round(abs(delta_sz), 6),
            "px": round(px, 6), "fee": round(fee, 4), "realized": round(realized, 2)}


def _accrue_funding(st: dict) -> None:
    """Hourly funding at the current rate for the time since the last accrual."""
    now = time.time()
    last = st.get("last_funding_ts")
    st["last_funding_ts"] = now
    meta, ctxs = post_json_with_retry(hl.INFO_URL, {"type": "metaAndAssetCtxs"})
    rate = {a["name"]: (float(c.get("funding") or 0), float(c.get("markPx") or 0))
            for a, c in zip(meta["universe"], ctxs)}
    funding = {k: v[0] for k, v in rate.items()}
    if not last or not st["positions"]:
        return funding
    hours = (now - last) / 3600
    paid = 0.0
    for coin, p in st["positions"].items():
        r, mark = rate.get(coin, (0.0, 0.0))
        paid += p["szi"] * mark * r * hours   # longs pay positive funding, shorts receive it
    st["cash"] -= paid
    st["costs"]["funding"] += paid
    return funding


def _note_gap(st: dict) -> None:
    """If the last cycle is older than GAP_S the bot was offline; log it. The cycle that follows
    re-syncs to the leaders' positions right away (it always rebalances to their current book)."""
    now = time.time()
    last = st.get("last_cycle_ts")
    if last and now - last > GAP_S:
        hours = (now - last - INTERVAL_S) / 3600
        st["gaps"].append({"from": datetime.fromtimestamp(last, timezone.utc).isoformat(timespec="minutes"),
                           "to": _now().isoformat(timespec="minutes"), "hours": round(hours, 2)})
        log.warning("CopyBot was offline %.1f h — re-syncing to the leaders now", hours)
    st["last_cycle_ts"] = now


def _stats(st: dict, eq: float) -> dict:
    import math
    start = datetime.fromisoformat(st["started_at"])
    days = (_now() - start).total_seconds() / 86400
    vals = [v for _, v in sorted(st.get("daily", {}).items())]
    rets = [b / a - 1 for a, b in zip(vals, vals[1:]) if a > 0]
    sharpe = None
    if len(rets) >= 5:
        m = sum(rets) / len(rets)
        sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        sharpe = round(m / sd * math.sqrt(365), 2) if sd > 0 else None
    btc_ret = None
    marks = st.get("last_marks", {})
    btc_now = st.get("btc_last")
    if st.get("btc_start") and btc_now:
        btc_ret = round((btc_now / st["btc_start"] - 1) * 100, 2)
    return {
        "days_running": round(days, 2),
        "gaps": len(st.get("gaps", [])),
        "gap_hours": round(sum(g["hours"] for g in st.get("gaps", [])), 2),
        "gap_list": st.get("gaps", [])[-10:],
        "max_drawdown_pct": round((st.get("max_dd") or 0) * 100, 2),
        "sharpe": sharpe,
        "return_pct": round((eq / st["starting_capital"] - 1) * 100, 2),
        "btc_hold_return_pct": btc_ret,
        "pnl_total": round(eq - st["starting_capital"], 2),
    }


def run_cycle(force_rerank: bool = False) -> dict:
    with _lock:
        _status["running_cycle"] = True
        try:
            st = _load()
            _note_gap(st)
            funding = _accrue_funding(st)
            wallets = hl.select_wallets({"ranking": RANKING}, st["hl"], force=force_rerank)
            states = {}
            for w in wallets:
                try:
                    states[w["address"]] = hl.user_state(w["address"])
                except Exception as e:
                    log.warning("wallet %s: %s", w["address"], e)
            mids = hl.all_mids()
            meta = hl.perp_meta()
            if st.get("btc_start") is None and mids.get("BTC"):
                st["btc_start"] = mids["BTC"]
            st["btc_last"] = mids.get("BTC")
            eq = _equity(st, mids)

            today = _now().strftime("%Y-%m-%d")
            if st["day"]["date"] != today:
                st["day"] = {"date": today, "start_equity": eq, "halted": False}
            if eq < st["day"]["start_equity"] * (1 - MAX_DAILY_LOSS):
                st["day"]["halted"] = True
            targets = {} if st["day"]["halted"] else hl.blend_targets(states, eq, mids, set(meta), SIZING)
            current = {c: p["szi"] for c, p in st["positions"].items()}
            orders = hl.plan_orders(targets, current, mids, meta, SIZING)
            stamp = _now().strftime("%m-%d %H:%M")
            for o in orders:
                delta = o["sz"] if o["is_buy"] else -o["sz"]
                if o["close"]:
                    delta = -current.get(o["coin"], 0.0)
                tr = _apply_fill(st, o["coin"], delta, mids[o["coin"]])
                tr["t"] = stamp
                st["trades"].insert(0, tr)
            st["trades"] = st["trades"][:400]
            eq = _equity(st, mids)
            st["equity_curve"].append([stamp, round(eq, 2)])
            st["equity_curve"] = st["equity_curve"][-2000:]
            st["peak"] = max(st.get("peak") or eq, eq)
            if st["peak"] > 0:
                st["max_dd"] = max(st.get("max_dd") or 0.0, (st["peak"] - eq) / st["peak"])
            st.setdefault("daily", {})[_now().strftime("%Y-%m-%d")] = round(eq, 2)
            st["last_marks"] = {c: mids.get(c) for c in st["positions"]}
            st["wallets"] = wallets
            _save(st)
            _status.update(last_run=_now().isoformat(timespec="seconds"), last_error=None)
            us = {}
            try:
                us = copy_live.on_cycle(states, mids, set(meta), funding)
            except Exception as e:
                log.warning("US books: %s", e)
                us = {"error": str(e)}
            return {"orders": len(orders), "equity": round(eq, 2), "us": us}
        except Exception as e:
            _status["last_error"] = str(e)
            log.warning("copy sim cycle failed: %s", e)
            raise
        finally:
            _status["running_cycle"] = False


def _loop():
    while True:
        try:
            run_cycle()
        except Exception:
            pass
        time.sleep(INTERVAL_S)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="copy-sim")
    _thread.start()


def reset() -> None:
    with _lock:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE.replace(".json", f"_{int(time.time())}.json"))


def summary() -> dict:
    st = _load()
    marks = st.get("last_marks", {})
    unreal = sum(p["szi"] * ((marks.get(c) or p["entry"]) - p["entry"]) for c, p in st["positions"].items())
    eq = st["cash"] + unreal
    costs = {k: round(v, 2) for k, v in st["costs"].items()}
    gross_exposure = sum(abs(p["szi"]) * (marks.get(c) or p["entry"]) for c, p in st["positions"].items())
    return {
        "mode": "dry_run",
        "record_version": st.get("version"),
        "stats": _stats(st, eq),
        "started_at": st["started_at"],
        "last_run": _status["last_run"] or (st["equity_curve"][-1][0] if st["equity_curve"] else None),
        "last_error": _status["last_error"],
        "running_cycle": _status["running_cycle"],
        "starting_capital": st["starting_capital"],
        "equity": round(eq, 2),
        "pnl_total": round(eq - st["starting_capital"], 2),
        "pnl_before_costs": round(eq - st["starting_capital"] + sum(st["costs"].values()), 2),
        "costs": costs,
        "costs_total": round(sum(st["costs"].values()), 2),
        "unrealized": round(unreal, 2),
        "leverage": round(gross_exposure / eq, 2) if eq > 0 else 0,
        "halted_today": st["day"].get("halted", False),
        "wallets": st.get("wallets", []),
        "positions": [{"coin": c, "size": round(p["szi"], 6), "entry": round(p["entry"], 6),
                       "mark": marks.get(c), "notional": round(abs(p["szi"]) * (marks.get(c) or p["entry"]), 2),
                       "side": "LONG" if p["szi"] > 0 else "SHORT"} for c, p in
                      sorted(st["positions"].items(), key=lambda kv: -abs(kv[1]["szi"] * kv[1]["entry"]))],
        "recent_trades": st["trades"][:30],
        "equity_curve": st["equity_curve"][-300:],
    }
