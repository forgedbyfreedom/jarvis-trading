# Vendored from github.com/forgedbyfreedom/copybot (read-only data + sizing logic; no order code).
"""Hyperliquid: rank top wallets by ROI and compute a blended copy portfolio."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .core import get_with_retry, post_json_with_retry, utcnow

log = logging.getLogger("copybot.hl")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class Candidate:
    address: str
    roi: float
    pnl: float
    volume: float
    account_value: float
    alltime_roi: float
    week_roi: float
    display_name: str | None = None


def _windows(row: dict) -> dict[str, dict[str, float]]:
    out = {}
    for name, perf in row.get("windowPerformances", []):
        out[name] = {k: float(v) for k, v in perf.items()}
    return out


def screen_leaderboard(rows: list[dict], rcfg: dict[str, Any]) -> list[Candidate]:
    """Apply the quality filters and sort by ROI over the configured window (highest first)."""
    window = rcfg.get("window", "month")
    excluded = {a.lower() for a in rcfg.get("exclude_addresses", [])}
    out: list[Candidate] = []
    for row in rows:
        addr = row.get("ethAddress", "").lower()
        if not addr or addr in excluded:
            continue
        w = _windows(row)
        if window not in w or "allTime" not in w:
            continue
        acct = float(row.get("accountValue") or 0)
        roi, vol = w[window]["roi"], w[window]["vlm"]
        if acct < rcfg.get("min_account_value", 0):
            continue
        if vol < rcfg.get("min_window_volume", 0):
            continue
        if acct > 0 and vol / acct > rcfg.get("max_turnover", float("inf")):
            continue
        if roi <= 0 or roi > rcfg.get("max_window_roi", float("inf")):
            continue
        if rcfg.get("require_positive_alltime", True) and (w["allTime"]["pnl"] <= 0 or w["allTime"]["roi"] <= 0):
            continue
        if rcfg.get("require_positive_week", True) and w.get("week", {}).get("pnl", 0) <= 0:
            continue
        out.append(
            Candidate(
                address=addr,
                roi=roi,
                pnl=w[window]["pnl"],
                volume=vol,
                account_value=acct,
                alltime_roi=w["allTime"]["roi"],
                week_roi=w.get("week", {}).get("roi", 0.0),
                display_name=row.get("displayName"),
            )
        )
    out.sort(key=lambda c: c.roi, reverse=True)
    return out


def fetch_leaderboard() -> list[dict]:
    return get_with_retry(LEADERBOARD_URL, timeout=60).json()["leaderboardRows"]


def user_state(address: str) -> dict:
    return post_json_with_retry(INFO_URL, {"type": "clearinghouseState", "user": address})


def all_mids() -> dict[str, float]:
    return {k: float(v) for k, v in post_json_with_retry(INFO_URL, {"type": "allMids"}).items()}


def perp_meta() -> dict[str, dict]:
    """coin -> {szDecimals, maxLeverage} for tradable (non-delisted) perps."""
    meta = post_json_with_retry(INFO_URL, {"type": "meta"})
    return {
        a["name"]: {"szDecimals": a["szDecimals"], "maxLeverage": a.get("maxLeverage", 1)}
        for a in meta["universe"]
        if not a.get("isDelisted")
    }


def positions_of(state: dict) -> dict[str, float]:
    """coin -> signed size (szi)."""
    out = {}
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi") or 0)
        if szi:
            out[p["coin"]] = szi
    return out


def account_value(state: dict) -> float:
    return float(state.get("marginSummary", {}).get("accountValue") or 0)


def select_wallets(cfg: dict[str, Any], st: dict[str, Any], *, force: bool = False) -> list[dict]:
    """Return the cached wallet selection, re-ranking when stale."""
    rcfg = cfg["ranking"]
    sel = st.get("selection")
    if sel and not force:
        age = utcnow() - _parse_ts(sel["ranked_at"])
        if age < timedelta(hours=rcfg.get("rerank_hours", 24)):
            return sel["wallets"]

    log.info("re-ranking Hyperliquid wallets")
    cands = screen_leaderboard(fetch_leaderboard(), rcfg)
    log.info("%d wallets passed filters", len(cands))

    chosen: list[dict] = []
    forced = [a.lower() for a in rcfg.get("always_include_addresses", [])]
    for addr in forced:
        chosen.append({"address": addr, "roi": None, "reason": "always_include"})

    # Verify the top candidates against their live clearinghouse account (the leaderboard
    # account value can include spot/vault balances that aren't backing perp positions).
    for c in cands[: rcfg.get("candidates_to_inspect", 40)]:
        if len(chosen) >= rcfg.get("top_n", 5):
            break
        if c.address in forced:
            continue
        try:
            s = user_state(c.address)
        except Exception as e:  # noqa: BLE001
            log.warning("skip %s: %s", c.address, e)
            continue
        av = account_value(s)
        if av < rcfg.get("min_account_value", 0):
            continue
        chosen.append(
            {
                "address": c.address,
                "roi": round(c.roi, 4),
                "alltime_roi": round(c.alltime_roi, 4),
                "perp_account_value": round(av, 2),
                "name": c.display_name,
                "reason": f"{rcfg.get('window', 'month')} ROI rank",
            }
        )
    st["selection"] = {"ranked_at": utcnow().isoformat(), "wallets": chosen}
    return chosen


def _parse_ts(s: str):
    from datetime import datetime

    return datetime.fromisoformat(s)


def blend_targets(
    wallet_states: dict[str, dict],
    my_equity: float,
    mids: dict[str, float],
    tradable: set[str],
    scfg: dict[str, Any],
) -> dict[str, float]:
    """Target notional (signed USD) per coin.

    Each copied wallet gets an equal slice of (equity * capital_fraction). Within a slice we
    reproduce that wallet's exposure as a fraction of its own account value, so a wallet at
    2x long BTC becomes 2x the slice long BTC. Results are then capped by gross leverage and
    per-coin limits.
    """
    if not wallet_states or my_equity <= 0:
        return {}
    slice_usd = my_equity * scfg.get("capital_fraction", 0.5) / len(wallet_states)
    targets: dict[str, float] = {}
    for addr, s in wallet_states.items():
        av = account_value(s)
        if av <= 0:
            continue
        for coin, szi in positions_of(s).items():
            if coin not in tradable or coin not in mids:
                continue
            exposure = szi * mids[coin] / av  # signed leverage on this coin
            targets[coin] = targets.get(coin, 0.0) + exposure * slice_usd

    # Keep only the biggest exposures; dozens of $20 positions just bleed fees.
    max_coins = scfg.get("max_coins")
    if max_coins and len(targets) > max_coins:
        keep = sorted(targets, key=lambda c: abs(targets[c]), reverse=True)[:max_coins]
        targets = {c: targets[c] for c in keep}

    max_lev = scfg.get("max_gross_leverage", 3.0)
    coin_cap = my_equity * max_lev * scfg.get("max_coin_fraction", 0.35)
    targets = {c: max(-coin_cap, min(coin_cap, v)) for c, v in targets.items()}
    gross = sum(abs(v) for v in targets.values())
    if gross > my_equity * max_lev:
        k = my_equity * max_lev / gross
        targets = {c: v * k for c, v in targets.items()}
    return {c: v for c, v in targets.items() if abs(v) >= 1e-9}


def plan_orders(
    targets: dict[str, float],
    current_sizes: dict[str, float],
    mids: dict[str, float],
    meta: dict[str, dict],
    scfg: dict[str, Any],
) -> list[dict]:
    """Diff target notional vs current position -> list of {coin, is_buy, sz, notional, reduce_only}."""
    orders = []
    min_usd = scfg.get("min_trade_usd", 15)
    band = scfg.get("rebalance_band", 0.15)
    for coin in sorted(set(targets) | set(current_sizes)):
        if coin not in mids or coin not in meta:
            continue
        px = mids[coin]
        cur = current_sizes.get(coin, 0.0) * px
        tgt = targets.get(coin, 0.0)
        delta = tgt - cur
        closing = abs(tgt) < min_usd and abs(cur) > 0
        if not closing:
            if abs(delta) < min_usd:
                continue
            if abs(tgt) > 0 and abs(delta) < band * abs(tgt):
                continue
        dec = meta[coin]["szDecimals"]
        sz = round(abs(delta) / px, dec)
        if closing:
            sz = round(abs(current_sizes.get(coin, 0.0)), dec)
        if sz <= 0 or sz * px < 10:  # exchange minimum ~$10
            continue
        orders.append(
            {
                "coin": coin,
                "is_buy": delta > 0,
                "sz": sz,
                "notional": round(sz * px, 2),
                "target": round(tgt, 2),
                "current": round(cur, 2),
                "close": closing,
            }
        )
    # Reduce exposure first so margin is freed before adding.
    orders.sort(key=lambda o: 0 if (o["close"] or abs(o["target"]) < abs(o["current"])) else 1)
    return orders
