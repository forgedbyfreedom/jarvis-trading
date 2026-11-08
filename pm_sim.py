"""
Polymarket US 5-minute Up/Down market-maker, DRY RUN ONLY.

Every few seconds it reads the live order book for the current BTC/ETH/SOL/XRP
5-minute markets and keeps one simulated bid and one simulated offer on the Up
contract, joining the best price on each side. Fills are counted only when the
market trades THROUGH our price (strictly better than our quote), which assumes
we're at the back of the queue. Positions are held to settlement ($1 or $0 per
Up contract). Makers pay no fee on Polymarket US, so the costs that matter here
are adverse selection and settlement losses; any taker exit would pay the
crypto taker fee in fees.polymarket_taker_fee.

It never places a real order: there is no order code in this module.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import config as C

log = logging.getLogger("jarvis.pm")

GATEWAY = "https://gateway.polymarket.us"
COINS = ["btc", "eth", "sol", "xrp"]
STATE_FILE = os.path.join(C.DATA_DIR, "pm_sim.json")

QUOTE_SIZE = 10            # contracts per side
MAX_INVENTORY = 50         # |net Up contracts| per market before we stop adding
MIN_SPREAD = 0.01          # quote any uncrossed book (BTC is usually 1 cent wide)
START_DELAY_S = 15         # skip the first seconds of a window
STOP_BEFORE_END_S = 60     # stop quoting in the last minute (prices jump to 0/1)
POLL_S = 2                 # pause between sweeps (each sweep takes ~3s at MIN_GAP_S)

_lock = threading.RLock()
_thread = None
_running = False
_status = {"last_poll": None, "errors": 0, "books": {}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


_slug_cache: dict[str, str | None] = {}


def _slug(coin: str, start: datetime) -> str | None:
    """Market slug for a coin's 5-minute window (prefixes differ by coin, so ask the event)."""
    ev = f"{coin}-updown-5m-{start.strftime('%Y-%m-%d-%H%Mz')}"
    if ev not in _slug_cache:
        data = _get(f"/v1/events/slug/{ev}")
        mk = (data or {}).get("event", {}).get("markets", [])
        _slug_cache[ev] = mk[0]["slug"] if mk else None
        if len(_slug_cache) > 200:
            for k in list(_slug_cache)[:100]:
                _slug_cache.pop(k, None)
    return _slug_cache[ev]


def _window_start(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0, minute=t.minute // 5 * 5)


_backoff_until = 0.0
_last_req = 0.0
MIN_GAP_S = 1.6            # ~37 requests/min; Polymarket's public gateway 429s above ~1/s


def _get(path: str):
    global _backoff_until, _last_req
    if time.time() < _backoff_until:
        raise RuntimeError("rate-limited, backing off")
    wait = _last_req + MIN_GAP_S - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_req = time.time()
    r = requests.get(GATEWAY + path, timeout=8)
    if r.status_code == 429:
        _backoff_until = time.time() + 30
        _status["rate_limited"] = _status.get("rate_limited", 0) + 1
        raise RuntimeError("429 from Polymarket; pausing 30s")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _px(a) -> float | None:
    return float(a["value"]) if a and a.get("value") is not None else None


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "started_at": _now().isoformat(timespec="seconds"),
            "markets": {},      # slug -> {coin, end, pos, cash, bid, ask, fills, last_shares}
            "settled": [],      # closed-market results, newest first
            "totals": {"pnl": 0.0, "fills": 0, "volume_contracts": 0, "markets": 0,
                       "fees": 0.0, "wins": 0, "losses": 0},
        }


def _save(st: dict) -> None:
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def _fill(m: dict, side: str, price: float, qty: int) -> None:
    if side == "buy":
        m["pos"] += qty
        m["cash"] -= price * qty
    else:  # sell Up (equivalent to buying Down at 1 - price)
        m["pos"] -= qty
        m["cash"] += price * qty
    m["fills"].append({"t": _now().strftime("%H:%M:%S"), "side": side, "px": price, "qty": qty})
    m["volume"] = m.get("volume", 0) + qty


def _step_market(st: dict, coin: str, start: datetime) -> None:
    slug = _slug(coin, start)
    if not slug:
        return
    end = start + timedelta(minutes=5)
    m = st["markets"].setdefault(slug, {"coin": coin, "end": end.isoformat(), "pos": 0, "cash": 0.0,
                                        "bid": None, "ask": None, "fills": [], "last_shares": None,
                                        "last_trade": None})
    data = _get(f"/v1/markets/{slug}/bbo")
    if not data:
        return
    md = data["marketData"]
    bid, ask = _px(md.get("bestBid")), _px(md.get("bestAsk"))
    last = _px(md.get("lastTradePx"))
    shares = float(md.get("sharesTraded") or 0)
    traded = m["last_shares"] is not None and shares > m["last_shares"]
    _status["books"][coin] = {"bid": bid, "ask": ask, "last": last, "slug": slug}

    # 1) Did the market trade through our resting quotes since the last look?
    if m["bid"] is not None and ((ask is not None and ask < m["bid"]) or (traded and last is not None and last < m["bid"])):
        _fill(m, "buy", m["bid"], QUOTE_SIZE)
        m["bid"] = None
    if m["ask"] is not None and ((bid is not None and bid > m["ask"]) or (traded and last is not None and last > m["ask"])):
        _fill(m, "sell", m["ask"], QUOTE_SIZE)
        m["ask"] = None
    m["last_shares"] = shares
    m["last_trade"] = last

    # 2) Re-quote (join best bid / best offer) when the window and book allow it.
    now = _now()
    live = start + timedelta(seconds=START_DELAY_S) <= now <= end - timedelta(seconds=STOP_BEFORE_END_S)
    if not live or bid is None or ask is None or ask - bid < MIN_SPREAD - 1e-9:
        m["bid"] = m["ask"] = None
        return
    m["bid"] = bid if m["pos"] < MAX_INVENTORY else None
    m["ask"] = ask if m["pos"] > -MAX_INVENTORY else None


def _settle(st: dict) -> None:
    now = _now()
    for slug, m in list(st["markets"].items()):
        end = datetime.fromisoformat(m["end"])
        if now < end + timedelta(seconds=20):
            continue
        if not m["fills"]:
            if now > end + timedelta(minutes=10):
                st["markets"].pop(slug)
            continue
        if time.time() < m.get("next_settle_check", 0):
            continue
        m["next_settle_check"] = time.time() + 30
        try:
            s = _get(f"/v1/markets/{slug}/settlement")
        except RuntimeError:
            continue  # rate-limited; try again next sweep
        if s is None:
            if now > end + timedelta(hours=2):
                log.warning("no settlement for %s after 2h; dropping", slug)
                st["markets"].pop(slug)
            continue
        payout = float(s["settlement"])  # 1 = Up won, 0 = Down won
        pnl = round(m["cash"] + m["pos"] * payout, 4)
        t = st["totals"]
        t["pnl"] = round(t["pnl"] + pnl, 4)
        t["fills"] += len(m["fills"])
        t["volume_contracts"] += m.get("volume", 0)
        t["markets"] += 1
        t["wins" if pnl > 0 else "losses"] += 1
        st["settled"].insert(0, {"slug": slug, "coin": m["coin"], "result": "UP" if payout >= 0.5 else "DOWN",
                                 "fills": len(m["fills"]), "end_pos": m["pos"], "pnl": pnl})
        st["settled"] = st["settled"][:300]
        st["markets"].pop(slug)


def _loop() -> None:
    global _running
    _running = True
    log.info("Polymarket US market-maker simulator started (dry run, no orders)")
    while _running:
        try:
            with _lock:
                st = _load()
                start = _window_start(_now())
                for coin in COINS:
                    if time.time() < _backoff_until:
                        break  # cooling off after a 429; don't count these as errors
                    try:
                        _step_market(st, coin, start)
                    except Exception as e:  # one coin failing shouldn't stop the rest
                        _status["errors"] += 1
                        log.debug("pm %s: %s", coin, e)
                _settle(st)
                _save(st)
                _status["last_poll"] = _now().isoformat(timespec="seconds")
        except Exception as e:
            _status["errors"] += 1
            log.warning("pm loop: %s", e)
        time.sleep(POLL_S)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="pm-sim")
    _thread.start()


def stop() -> None:
    global _running
    _running = False


def reset() -> None:
    with _lock:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE.replace(".json", f"_{int(time.time())}.json"))


def summary() -> dict:
    with _lock:
        st = _load()
    t = st["totals"]
    open_mk = [{"slug": s, "coin": m["coin"], "pos": m["pos"], "bid": m["bid"], "ask": m["ask"],
                "fills": len(m["fills"]), "cash": round(m["cash"], 2)} for s, m in st["markets"].items()]
    n = t["markets"]
    return {
        "running": bool(_thread and _thread.is_alive()),
        "mode": "dry_run",
        "started_at": st["started_at"],
        "last_poll": _status["last_poll"],
        "errors": _status["errors"],
        "rate_limited": _status.get("rate_limited", 0),
        "books": _status["books"],
        "totals": {**t, "pnl": round(t["pnl"], 2),
                   "win_rate": round(100 * t["wins"] / n, 1) if n else 0,
                   "pnl_per_market": round(t["pnl"] / n, 3) if n else 0},
        "open": open_mk,
        "recent": st["settled"][:25],
        "settings": {"quote_size": QUOTE_SIZE, "max_inventory": MAX_INVENTORY,
                     "min_spread": MIN_SPREAD, "fees": "Maker 0% on Polymarket US; no taker orders"},
    }
