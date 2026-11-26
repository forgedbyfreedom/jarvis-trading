"""
CopyBot US execution layer.

Every CopyBot cycle (copy_sim.run_cycle) hands this module the leaders' current positions.
From them it runs two books on the US-legal venues (Kalshi perps first, Coinbase US perps second):

  SHADOW  Always on. A paper account that trades exactly what the live bot would: whole
          contracts at each venue's contract size, each venue's taker fee and minimum, coins
          that aren't listed in the US skipped. This is the honest preview of live results.

  LIVE    Off until Bryan arms it in the Command Center. Same routing and sizing, but sent to
          the venues with his own API keys (trade-only, no withdraw). Hard guard rails:
            * capital cap (the bot never sizes off more than this, whatever the balance is)
            * max gross leverage (default 2x, the sim uses 3x)
            * daily loss stop: down X% on the day -> flatten everything and disarm
            * per-order size cap, reduce-only exits, IOC orders that can't rest or run away
            * three failed cycles in a row -> disarm
            * kill switch: cancel all + flatten + disarm, one click
The rules decide; there is no discretionary override inside a cycle. That is the point.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

import config as C
import us_venues as V
from copybot_lib import hl_source as hl

log = logging.getLogger("jarvis.copylive")

STATE_FILE = os.path.join(C.DATA_DIR, "copy_live.json")
_lock = threading.RLock()

# Sizing for the US books. Same wallet blend as the sim, a little more conservative.
SIZING_US = {
    "capital_fraction": 0.5, "max_gross_leverage": 2.0, "max_coin_fraction": 0.35,
    "max_coins": 10, "min_trade_usd": 5, "rebalance_band": 0.15,
}
SHADOW_CAPITAL = getattr(C, "COPY_LIVE_SHADOW_CAPITAL", 500.0)
HARD_MAX_CAPITAL = getattr(C, "COPY_LIVE_HARD_MAX_CAPITAL", 5000.0)   # raise in config.py to go bigger
DEFAULTS = {"capital_cap": 500.0, "max_leverage": 2.0, "daily_loss_pct": 5.0, "venues": ["kalshi", "coinbase"]}

READINESS_RULES = {
    "min_days": 21,            # 3 weeks of clean record
    "max_gap_hours": 6,        # total hours the bot was offline in the record
    "max_drawdown_pct": 20,
    "min_coverage_pct": 60,    # share of the leaders' exposure tradable on US venues
}


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().strftime("%m-%d %H:%M")


def _fresh() -> dict:
    return {
        "version": 1,
        "shadow": {
            "started_at": _now().isoformat(timespec="seconds"), "starting_capital": SHADOW_CAPITAL,
            "cash": SHADOW_CAPITAL, "positions": {}, "costs": {"trading_fee": 0.0, "slippage": 0.0, "funding": 0.0},
            "trades": [], "equity_curve": [], "peak": SHADOW_CAPITAL, "max_dd": 0.0, "skipped": [],
            "coverage": None, "last_funding_ts": None, "daily": {},
        },
        "live": {
            "armed": False, "armed_at": None, "settings": dict(DEFAULTS),
            "day": {"date": None, "start_equity": None, "halted": False},
            "trades": [], "events": [], "errors_in_row": 0, "last_cycle": None, "last_error": None,
            "equity": None, "equity_curve": [], "venue_balances": {}, "positions": {}, "connections": {},
        },
    }


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        base = _fresh()
        for k in ("shadow", "live"):
            for kk, vv in base[k].items():
                st[k].setdefault(kk, vv)
        for kk, vv in DEFAULTS.items():
            st["live"]["settings"].setdefault(kk, vv)
        return st
    except Exception:
        return _fresh()


def _save(st: dict) -> None:
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def _event(st: dict, kind: str, msg: str) -> None:
    st["live"]["events"].insert(0, {"t": _now().isoformat(timespec="seconds"), "kind": kind, "msg": msg})
    st["live"]["events"] = st["live"]["events"][:200]
    log.warning("copy-live %s: %s", kind, msg)


# ───────────────────────────────────────────── routing (shared by shadow and live)
def route(targets: dict[str, float], held: dict[str, dict], mk: dict[str, dict[str, dict]],
          venues: list[str]) -> tuple[dict[str, dict], list[dict]]:
    """targets: HL coin -> signed USD.  held: norm coin -> {venue, symbol, contracts}.
    Returns (norm coin -> {venue, symbol, mk, contracts}, skipped list)."""
    plan, skipped = {}, []
    for coin, usd in targets.items():
        c = V.norm_coin(coin)
        choice = None
        h = held.get(c)
        if h and h["venue"] in venues and c in mk.get(h["venue"], {}):
            choice = h["venue"]                      # don't hop venues on an open position
        else:
            for v in V.VENUE_ORDER:
                if v in venues and c in mk.get(v, {}) and mk[v][c].get("open", True):
                    choice = v
                    break
        if not choice:
            skipped.append({"coin": c, "target": round(usd, 2), "why": "not listed on an enabled US venue"})
            continue
        m = mk[choice][c]
        n = usd / m["px_contract"]
        contracts = math.copysign(math.floor(abs(n)), n) if abs(n) >= m["min_qty"] else 0.0
        if contracts == 0:
            skipped.append({"coin": c, "target": round(usd, 2),
                            "why": f"below 1 contract (${m['px_contract']:.2f} on {V.FEES[choice]['name']})"})
        plan[c] = {"venue": choice, "symbol": m["symbol"], "mk": m, "contracts": contracts, "target": usd}
    return plan, skipped


def diff_orders(plan: dict[str, dict], held: dict[str, dict], mk: dict, band: float, min_usd: float) -> list[dict]:
    orders = []
    for c in sorted(set(plan) | set(held)):
        h = held.get(c)
        cur = h["contracts"] if h else 0.0
        p = plan.get(c)
        if p is None:                              # leaders no longer hold it -> close
            if cur and h["venue"] in mk and c in mk[h["venue"]]:
                orders.append({"coin": c, "venue": h["venue"], "symbol": h["symbol"], "mk": mk[h["venue"]][c],
                               "delta": -cur, "reduce": True, "close": True})
            continue
        if h and h["venue"] != p["venue"]:          # shouldn't happen (route keeps venue) — close old leg
            orders.append({"coin": c, "venue": h["venue"], "symbol": h["symbol"], "mk": mk[h["venue"]][c],
                           "delta": -cur, "reduce": True, "close": True})
            cur = 0.0
        delta = p["contracts"] - cur
        if abs(delta) < 1:
            continue
        notional = abs(delta) * p["mk"]["px_contract"]
        closing = p["contracts"] == 0 and cur != 0
        if not closing:
            if notional < min_usd:
                continue
            if p["contracts"] and abs(delta) < band * abs(p["contracts"]):
                continue
        reduce = cur != 0 and (abs(p["contracts"]) < abs(cur) or (p["contracts"] > 0) != (cur > 0))
        orders.append({"coin": c, "venue": p["venue"], "symbol": p["symbol"], "mk": p["mk"],
                       "delta": delta, "reduce": reduce and abs(delta) <= abs(cur), "close": closing})
    orders.sort(key=lambda o: 0 if o["reduce"] else 1)   # free margin first
    return orders


# ───────────────────────────────────────────── shadow book
def _shadow_marks(sh: dict, mk: dict) -> dict[str, float]:
    marks = {}
    for c, p in sh["positions"].items():
        m = mk.get(p["venue"], {}).get(c)
        marks[c] = m["px_contract"] if m else p.get("mark", p["entry"])
    return marks


def _shadow_equity(sh: dict, marks: dict) -> float:
    return sh["cash"] + sum(p["contracts"] * (marks.get(c, p["entry"]) - p["entry"]) for c, p in sh["positions"].items())


def _shadow_fill(sh: dict, o: dict) -> dict:
    v, m, d = o["venue"], o["mk"], o["delta"]
    slip = m["px_contract"] * V.FEES[v]["slippage_bps"] / 10_000
    px = m["px_contract"] + (slip if d > 0 else -slip)
    fee = V.trade_fee(v, abs(d) * px)
    c = o["coin"]
    pos = sh["positions"].get(c, {"venue": v, "symbol": o["symbol"], "contracts": 0.0, "entry": px})
    old, new = pos["contracts"], pos["contracts"] + d
    realized = 0.0
    if old and (old > 0) != (d > 0):
        realized = min(abs(d), abs(old)) * (px - pos["entry"]) * (1 if old > 0 else -1)
    if abs(new) < 1e-9:
        sh["positions"].pop(c, None)
    else:
        if old == 0 or (old > 0) != (new > 0):
            entry = px
        elif abs(new) > abs(old):
            entry = (pos["entry"] * abs(old) + px * abs(d)) / abs(new)
        else:
            entry = pos["entry"]
        sh["positions"][c] = {"venue": v, "symbol": o["symbol"], "contracts": new, "entry": entry, "mark": px}
    sh["cash"] += realized - fee
    sh["costs"]["trading_fee"] += fee
    sh["costs"]["slippage"] += abs(d) * slip
    return {"t": _stamp(), "coin": c, "venue": v, "symbol": o["symbol"], "side": "BUY" if d > 0 else "SELL",
            "contracts": abs(d), "px": round(px, 4), "fee": round(fee, 4), "realized": round(realized, 2)}


def _shadow_funding(sh: dict, funding: dict[str, float], marks: dict) -> None:
    """Hourly funding, using each coin's Hyperliquid rate as the proxy for the US venue's rate."""
    now = time.time()
    last = sh.get("last_funding_ts")
    sh["last_funding_ts"] = now
    if not last or not sh["positions"] or not funding:
        return
    hours = (now - last) / 3600
    paid = 0.0
    for c, p in sh["positions"].items():
        r = funding.get(c, 0.0)
        paid += p["contracts"] * marks.get(c, p["entry"]) * r * hours
    sh["cash"] -= paid
    sh["costs"]["funding"] += paid


def _update_curve(book: dict, eq: float) -> None:
    book["equity_curve"].append([_stamp(), round(eq, 2)])
    book["equity_curve"] = book["equity_curve"][-2000:]
    book["peak"] = max(book.get("peak") or eq, eq)
    if book["peak"] > 0:
        book["max_dd"] = max(book.get("max_dd") or 0.0, (book["peak"] - eq) / book["peak"])
    book.setdefault("daily", {})[_now().strftime("%Y-%m-%d")] = round(eq, 2)


def _norm_funding(funding_hl: dict[str, float]) -> dict[str, float]:
    return {V.norm_coin(k): v for k, v in (funding_hl or {}).items()}


def _coverage(full_targets: dict[str, float], mk: dict, venues: list[str]) -> float | None:
    gross = sum(abs(v) for v in full_targets.values())
    if gross <= 0:
        return None
    cov = sum(abs(v) for c, v in full_targets.items()
              if any(V.norm_coin(c) in mk.get(x, {}) for x in venues))
    return cov / gross


def run_shadow(st: dict, states: dict, mids: dict, tradable_hl: set, mk: dict, funding_hl: dict) -> dict:
    sh = st["shadow"]
    venues = st["live"]["settings"]["venues"]
    if not any(mk.get(v) for v in venues):
        raise RuntimeError("US venue market data unavailable this cycle — shadow book skipped")
    marks = _shadow_marks(sh, mk)
    _shadow_funding(sh, _norm_funding(funding_hl), marks)
    eq = _shadow_equity(sh, marks)
    full = hl.blend_targets(states, eq, mids, tradable_hl, SIZING_US)
    sh["coverage"] = _coverage(full, mk, venues)
    us_coins = {c for c in tradable_hl if any(V.norm_coin(c) in mk.get(v, {}) for v in venues)}
    targets = hl.blend_targets(states, eq, mids, us_coins, SIZING_US)
    held = {c: p for c, p in sh["positions"].items()}
    plan, skipped = route(targets, held, mk, venues)
    sh["skipped"] = skipped
    for o in diff_orders(plan, held, mk, SIZING_US["rebalance_band"], SIZING_US["min_trade_usd"]):
        sh["trades"].insert(0, _shadow_fill(sh, o))
    sh["trades"] = sh["trades"][:400]
    marks = _shadow_marks(sh, mk)
    eq = _shadow_equity(sh, marks)
    _update_curve(sh, eq)
    return {"equity": round(eq, 2), "positions": len(sh["positions"])}


# ───────────────────────────────────────────── live book
def _live_venues(st: dict) -> list[str]:
    return [v for v in st["live"]["settings"]["venues"] if V.keys_present(v)]


def _live_snapshot(venues: list[str], mk: dict) -> tuple[float, dict, dict, dict]:
    """Returns (total equity, balances, held positions in route() format, clients)."""
    total, bals, held, clients = 0.0, {}, {}, {}
    for v in venues:
        cl = V.client(v)
        clients[v] = cl
        b = cl.balance()
        bals[v] = b
        total += b["equity"]
        by_symbol = {m["symbol"]: c for c, m in mk.get(v, {}).items()}
        for sym, n in cl.positions().items():
            if abs(n) < 1e-9:
                continue
            c = by_symbol.get(sym)
            if c is None:
                continue                      # not a coin we trade (Bryan's own positions are left alone)
            held[c] = {"venue": v, "symbol": sym, "contracts": n}
    return total, bals, held, clients


def _flatten(st: dict, venues: list[str], mk: dict, reason: str) -> list[dict]:
    out = []
    for v in venues:
        try:
            cl = V.client(v)
            try:
                cl.cancel_all()
            except Exception as e:
                out.append({"venue": v, "error": f"cancel-all: {e}"})
            by_symbol = {m["symbol"]: c for c, m in mk.get(v, {}).items()}
            for sym, n in cl.positions().items():
                c = by_symbol.get(sym)
                if c is None or abs(n) < 1e-9:
                    continue
                try:
                    r = cl.market_order(mk[v][c], -n, reduce_only=True)
                    out.append({"venue": v, "symbol": sym, "closed": n, **r})
                except Exception as e:
                    out.append({"venue": v, "symbol": sym, "error": str(e)[:200]})
        except Exception as e:
            out.append({"venue": v, "error": str(e)[:200]})
    _event(st, "flatten", f"{reason}: " + json.dumps(out)[:600])
    return out


def run_live(st: dict, states: dict, mids: dict, tradable_hl: set, mk: dict) -> dict:
    lv = st["live"]
    if not lv["armed"]:
        return {"armed": False}
    venues = _live_venues(st)
    if not venues:
        lv["armed"] = False
        _event(st, "disarm", "No venue keys found — disarmed")
        return {"armed": False}
    s = lv["settings"]
    try:
        eq, bals, held, clients = _live_snapshot(venues, mk)
        lv["equity"], lv["venue_balances"] = round(eq, 2), bals
        today = _now().strftime("%Y-%m-%d")
        if lv["day"]["date"] != today:
            lv["day"] = {"date": today, "start_equity": eq, "halted": False}
        start = lv["day"]["start_equity"] or eq
        if start > 0 and eq < start * (1 - s["daily_loss_pct"] / 100):
            lv["day"]["halted"] = True
            _flatten(st, venues, mk, f"Daily loss stop ({s['daily_loss_pct']}%) hit: ${start:.2f} -> ${eq:.2f}")
            lv["armed"] = False
            _event(st, "disarm", "Daily loss stop — flat and disarmed. Re-arm manually after reviewing.")
            return {"armed": False, "halted": True}
        base = min(eq, float(s["capital_cap"]))
        sizing = dict(SIZING_US, max_gross_leverage=float(s["max_leverage"]))
        us_coins = {c for c in tradable_hl if any(V.norm_coin(c) in mk.get(v, {}) for v in venues)}
        targets = hl.blend_targets(states, base, mids, us_coins, sizing)
        plan, skipped = route(targets, held, mk, venues)
        max_order = base * float(s["max_leverage"]) * 0.5
        done = []
        for o in diff_orders(plan, held, mk, sizing["rebalance_band"], sizing["min_trade_usd"]):
            n = o["delta"]
            if not o["reduce"] and abs(n) * o["mk"]["px_contract"] > max_order:
                n = math.copysign(math.floor(max_order / o["mk"]["px_contract"]), n)
                if abs(n) < 1:
                    continue
            try:
                r = clients[o["venue"]].market_order(o["mk"], n, reduce_only=o["reduce"])
                done.append({"t": _stamp(), "coin": o["coin"], "venue": o["venue"], "symbol": o["symbol"],
                             "side": "BUY" if n > 0 else "SELL", "contracts": abs(n), **r})
            except Exception as e:
                done.append({"t": _stamp(), "coin": o["coin"], "venue": o["venue"], "error": str(e)[:300]})
        lv["trades"] = (done[::-1] + lv["trades"])[:400]
        failed = [d for d in done if d.get("error")]
        if failed and len(failed) == len(done):
            raise RuntimeError(failed[0]["error"])
        eq2, _, held2, _ = _live_snapshot(venues, mk)
        lv["equity"], lv["positions"] = round(eq2, 2), held2
        lv["skipped"] = skipped
        lv["equity_curve"].append([_stamp(), round(eq2, 2)])
        lv["equity_curve"] = lv["equity_curve"][-2000:]
        lv["errors_in_row"], lv["last_error"] = 0, None
        lv["last_cycle"] = _now().isoformat(timespec="seconds")
        return {"armed": True, "orders": len(done), "equity": round(eq2, 2)}
    except Exception as e:
        lv["errors_in_row"] += 1
        lv["last_error"] = str(e)[:400]
        _event(st, "error", lv["last_error"])
        if lv["errors_in_row"] >= 3:
            lv["armed"] = False
            _event(st, "disarm", "Three failed cycles in a row — disarmed")
        return {"armed": lv["armed"], "error": lv["last_error"]}


# ───────────────────────────────────────────── called by copy_sim every cycle
def on_cycle(states: dict, mids: dict, tradable_hl: set, funding_hl: dict) -> dict:
    with _lock:
        st = _load()
        mk = V.markets()
        out = {}
        try:
            out["shadow"] = run_shadow(st, states, mids, tradable_hl, mk, funding_hl)
        except Exception as e:
            log.warning("shadow book: %s", e)
            out["shadow"] = {"error": str(e)}
        out["live"] = run_live(st, states, mids, tradable_hl, mk)
        _save(st)
        return out


# ───────────────────────────────────────────── controls
def readiness(sim: dict, st: dict | None = None) -> dict:
    st = st or _load()
    sh = st["shadow"]
    R = READINESS_RULES
    sh_eq = sh["equity_curve"][-1][1] if sh["equity_curve"] else sh["starting_capital"]
    cov = sh.get("coverage")
    keyed = [v for v in V.VENUE_ORDER if V.keys_present(v)]
    conn = st["live"].get("connections", {})
    checks = [
        {"id": "days", "label": f"{R['min_days']}+ days of clean record", "ok": sim["days_running"] >= R["min_days"],
         "value": f"{sim['days_running']:.1f} days"},
        {"id": "gaps", "label": f"Bot offline under {R['max_gap_hours']} h total", "ok": sim["gap_hours"] <= R["max_gap_hours"],
         "value": f"{sim['gap_hours']:.1f} h in {sim['gaps']} gap(s)"},
        {"id": "dd", "label": f"Max drawdown under {R['max_drawdown_pct']}%", "ok": sim["max_drawdown_pct"] <= R["max_drawdown_pct"],
         "value": f"{sim['max_drawdown_pct']:.1f}%"},
        {"id": "sim_pnl", "label": "Sim profitable after every cost", "ok": sim["pnl_total"] > 0,
         "value": f"${sim['pnl_total']:,.2f}"},
        {"id": "shadow_pnl", "label": "US-venue shadow profitable after fees", "ok": sh_eq > sh["starting_capital"],
         "value": f"${sh_eq - sh['starting_capital']:,.2f}"},
        {"id": "coverage", "label": f"{R['min_coverage_pct']}%+ of leader exposure tradable in the US",
         "ok": cov is not None and cov * 100 >= R["min_coverage_pct"], "value": "–" if cov is None else f"{cov*100:.0f}%"},
        {"id": "keys", "label": "Trade-only API keys installed and tested",
         "ok": bool(keyed) and all(conn.get(v, {}).get("ok") for v in keyed),
         "value": ", ".join(f"{V.FEES[v]['name']} {'✓' if conn.get(v, {}).get('ok') else 'untested'}" for v in keyed) or "none yet"},
    ]
    return {"ready": all(c["ok"] for c in checks), "checks": checks}


def test_connections() -> dict:
    with _lock:
        st = _load()
        res = {}
        for v in V.VENUE_ORDER:
            r = V.test_connection(v)
            r["t"] = _now().isoformat(timespec="seconds")
            res[v] = r
            st["live"]["connections"][v] = {"ok": r["ok"], "t": r["t"], "error": r.get("error")}
        _save(st)
        return res


def update_settings(capital_cap=None, max_leverage=None, daily_loss_pct=None, venues=None) -> dict:
    with _lock:
        st = _load()
        s = st["live"]["settings"]
        if capital_cap is not None:
            cap = float(capital_cap)
            if not 0 < cap <= HARD_MAX_CAPITAL:
                raise ValueError(f"Capital cap must be between $1 and ${HARD_MAX_CAPITAL:,.0f} (raise COPY_LIVE_HARD_MAX_CAPITAL in config.py to go higher)")
            s["capital_cap"] = cap
        if max_leverage is not None:
            lev = float(max_leverage)
            if not 0.5 <= lev <= 3:
                raise ValueError("Max leverage must be 0.5x–3x")
            s["max_leverage"] = lev
        if daily_loss_pct is not None:
            d = float(daily_loss_pct)
            if not 1 <= d <= 15:
                raise ValueError("Daily loss stop must be 1%–15%")
            s["daily_loss_pct"] = d
        if venues is not None:
            vs = [v for v in venues if v in V.VENUE_ORDER]
            if not vs:
                raise ValueError("Pick at least one venue")
            s["venues"] = vs
        _event(st, "settings", json.dumps(s))
        _save(st)
        return s


def arm(sim: dict, confirm: str, override: str = "") -> dict:
    with _lock:
        st = _load()
        if confirm.strip().upper() != "ARM":
            raise ValueError('Type ARM to confirm')
        keyed = _live_venues(st)
        if not keyed:
            raise ValueError("No venue API keys in .env.local yet")
        conn = st["live"]["connections"]
        if not all(conn.get(v, {}).get("ok") for v in keyed):
            raise ValueError("Run Test connection first — every keyed venue must pass")
        rd = readiness(sim, st)
        if not rd["ready"] and override.strip().upper() != "OVERRIDE":
            raise ValueError("Readiness checklist not met. Type OVERRIDE in the second box to arm anyway.")
        st["live"]["armed"] = True
        st["live"]["armed_at"] = _now().isoformat(timespec="seconds")
        st["live"]["errors_in_row"] = 0
        st["live"]["day"] = {"date": None, "start_equity": None, "halted": False}
        _event(st, "arm", f"Armed on {', '.join(keyed)} · cap ${st['live']['settings']['capital_cap']:,.0f} · "
                          f"{st['live']['settings']['max_leverage']}x · stop {st['live']['settings']['daily_loss_pct']}%"
                          + ("" if rd["ready"] else " · readiness OVERRIDDEN"))
        _save(st)
        return {"armed": True}


def disarm(reason: str = "Disarmed from the Command Center") -> dict:
    with _lock:
        st = _load()
        st["live"]["armed"] = False
        _event(st, "disarm", reason + " (positions left open)")
        _save(st)
        return {"armed": False}


def kill() -> dict:
    with _lock:
        st = _load()
        st["live"]["armed"] = False
        venues = [v for v in V.VENUE_ORDER if V.keys_present(v)]
        res = _flatten(st, venues, V.markets(), "KILL SWITCH") if venues else []
        _event(st, "disarm", "Kill switch: cancelled orders, flattened, disarmed")
        _save(st)
        return {"armed": False, "results": res}


def reset_shadow() -> None:
    with _lock:
        st = _load()
        fresh = _fresh()
        if os.path.exists(STATE_FILE):
            arch = STATE_FILE.replace(".json", f"_shadow_{int(time.time())}.json")
            with open(arch, "w", encoding="utf-8") as f:
                json.dump(st["shadow"], f)
        st["shadow"] = fresh["shadow"]
        _save(st)


def summary(sim: dict) -> dict:
    st = _load()
    sh, lv = st["shadow"], st["live"]
    try:
        mk = V.markets()
    except Exception:
        mk = {}
    marks = _shadow_marks(sh, mk)
    eq = _shadow_equity(sh, marks)
    pos = [{"coin": c, "venue": V.FEES[p["venue"]]["name"], "symbol": p["symbol"], "contracts": p["contracts"],
            "side": "LONG" if p["contracts"] > 0 else "SHORT", "entry": round(p["entry"], 4),
            "mark": round(marks.get(c, p["entry"]), 4), "notional": round(abs(p["contracts"]) * marks.get(c, p["entry"]), 2),
            "pnl": round(p["contracts"] * (marks.get(c, p["entry"]) - p["entry"]), 2)}
           for c, p in sorted(sh["positions"].items(), key=lambda kv: -abs(kv[1]["contracts"] * kv[1]["entry"]))]
    return {
        "shadow": {
            "started_at": sh["started_at"], "starting_capital": sh["starting_capital"], "equity": round(eq, 2),
            "pnl_total": round(eq - sh["starting_capital"], 2), "costs": {k: round(v, 2) for k, v in sh["costs"].items()},
            "max_drawdown_pct": round((sh.get("max_dd") or 0) * 100, 1),
            "coverage_pct": None if sh.get("coverage") is None else round(sh["coverage"] * 100),
            "positions": pos, "skipped": sh.get("skipped", []), "recent_trades": sh["trades"][:30],
            "equity_curve": sh["equity_curve"][-300:],
        },
        "live": {
            "armed": lv["armed"], "armed_at": lv["armed_at"], "settings": lv["settings"],
            "hard_max_capital": HARD_MAX_CAPITAL, "equity": lv.get("equity"), "venue_balances": lv.get("venue_balances", {}),
            "positions": lv.get("positions", {}), "halted_today": lv["day"].get("halted", False),
            "last_cycle": lv.get("last_cycle"), "last_error": lv.get("last_error"), "recent_trades": lv["trades"][:30],
            "events": lv["events"][:30], "connections": lv.get("connections", {}),
            "keys": {v: V.keys_present(v) for v in V.VENUE_ORDER},
            "equity_curve": lv.get("equity_curve", [])[-300:],
        },
        "venues": {v: {"name": V.FEES[v]["name"], "fee": f"{V.FEES[v]['taker']*100:.2f}% taker"
                       + (f", ${V.FEES[v]['min_fee']:.2f} min" if V.FEES[v]["min_fee"] else ""),
                       "coins": sorted(mk.get(v, {}).keys())} for v in V.VENUE_ORDER},
        "readiness": readiness(sim, st),
        "rules": READINESS_RULES,
    }
