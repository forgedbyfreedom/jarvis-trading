"""
Jarvis cost model: every fee and friction a real order would pay.

Used by the dry-run simulator so simulated P&L is what you'd actually keep.
Rates verified Nov 2026 (sources in FEE_SCHEDULE["sources"]). Brokers change
fees; the settings live in config.py so they can be updated without code edits.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import config as C

ET = ZoneInfo("America/New_York")


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100.0


# ───────────────────────────── stocks (Alpaca)
def stock_sell_reg_fees(price: float, qty: float) -> dict:
    """Regulatory fees charged on every stock SALE (including selling short)."""
    principal = abs(price * qty)
    sec = _ceil_cent(principal * C.SEC_FEE_PER_MILLION / 1_000_000) if principal else 0.0
    sec = max(sec, 0.01) if principal else 0.0
    taf = min(max(_ceil_cent(abs(qty) * C.FINRA_TAF_PER_SHARE), 0.01), C.FINRA_TAF_MAX)
    return {"sec_fee": sec, "finra_taf": taf}


def stock_fill(side: str, ref_price: float, qty: float) -> dict:
    """Simulated market-order fill for a stock: pay half the spread plus slippage."""
    bps = C.STOCK_HALF_SPREAD_BPS + C.STOCK_SLIPPAGE_BPS
    adj = ref_price * bps / 10_000
    fill = ref_price + adj if side == "buy" else ref_price - adj
    costs = {"commission": C.STOCK_COMMISSION, "spread_slippage": round(adj * abs(qty), 4)}
    if side == "sell":
        costs.update(stock_sell_reg_fees(fill, qty))
    return {"fill_price": fill, "costs": costs}


def short_borrow_cost(entry_price: float, qty: float, days_held: float) -> float:
    """Stock-loan fee for a short position (easy-to-borrow default)."""
    return abs(entry_price * qty) * C.SHORT_BORROW_APR * days_held / 365.0


# ───────────────────────────── crypto spot
def crypto_fill(side: str, ref_price: float, qty: float) -> dict:
    venue = C.CRYPTO_VENUE
    v = C.CRYPTO_VENUES[venue]
    adj = ref_price * (v["half_spread_bps"] + v["slippage_bps"]) / 10_000
    fill = ref_price + adj if side == "buy" else ref_price - adj
    notional = abs(fill * qty)
    costs = {
        "trading_fee": round(notional * v["taker"], 4),  # market orders are taker
        "spread_slippage": round(adj * abs(qty), 4),
    }
    return {"fill_price": fill, "costs": costs, "venue": v["name"]}


# ───────────────────────────── generic
def fill(asset_type: str, side: str, ref_price: float, qty: float) -> dict:
    if asset_type == "stock":
        out = stock_fill(side, ref_price, qty)
        out["venue"] = "Alpaca"
    else:
        out = crypto_fill(side, ref_price, qty)
    out["total_cost"] = round(sum(out["costs"].values()), 4)
    return out


def us_market_open(now: datetime | None = None) -> bool:
    """Regular US equity session, Mon-Fri 9:30-16:00 ET (exchange holidays not modeled)."""
    now = (now or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 0)


def estimated_tax(net_realized: float) -> float:
    """Rough short-term capital-gains set-aside. Losses produce no refund here."""
    return round(max(0.0, net_realized) * C.EST_TAX_RATE, 2)


# ───────────────────────────── Hyperliquid perps (copybot)
def hyperliquid_trade_fee(notional: float, maker: bool = False) -> float:
    return abs(notional) * (C.HL_MAKER_FEE if maker else C.HL_TAKER_FEE)


# ───────────────────────────── Polymarket US
def polymarket_taker_fee(price: float, contracts: float, coef: float | None = None) -> float:
    """Polymarket US taker fee: coefficient x contracts x p x (1 - p). Makers pay 0."""
    coef = C.PM_TAKER_COEF_CRYPTO if coef is None else coef
    return round(coef * abs(contracts) * price * (1 - price), 4)


def schedule() -> dict:
    """Everything the dashboard shows in the 'Fees we model' card."""
    v = C.CRYPTO_VENUES[C.CRYPTO_VENUE]
    return {
        "stocks": {
            "venue": "Alpaca",
            "commission": "$0",
            "sec_fee": f"${C.SEC_FEE_PER_MILLION:.2f} per $1M sold (sells only, min $0.01)",
            "finra_taf": f"${C.FINRA_TAF_PER_SHARE} per share sold (min $0.01, max ${C.FINRA_TAF_MAX})",
            "spread_slippage": f"{C.STOCK_HALF_SPREAD_BPS + C.STOCK_SLIPPAGE_BPS} bps per side",
            "short_borrow": f"{C.SHORT_BORROW_APR * 100:.2f}%/yr on shorts (easy-to-borrow; hard-to-borrow can be far higher)",
            "hours": "Fills only 9:30-16:00 ET on weekdays",
        },
        "crypto": {
            "venue": v["name"],
            "taker_fee": f"{v['taker'] * 100:.2f}% per side (market orders)",
            "maker_fee": f"{v['maker'] * 100:.2f}% (limit orders that rest)",
            "spread_slippage": f"{v['half_spread_bps'] + v['slippage_bps']} bps per side",
            "shorting": "Not possible on spot; the simulator skips crypto short signals",
            "round_trip_break_even": f"{2 * (v['taker'] * 100 + (v['half_spread_bps'] + v['slippage_bps']) / 100):.2f}% move needed just to break even",
        },
        "hyperliquid": {
            "taker_fee": f"{C.HL_TAKER_FEE * 100:.3f}%",
            "maker_fee": f"{C.HL_MAKER_FEE * 100:.3f}%",
            "funding": "Charged/paid hourly on open perps at the live funding rate",
            "slippage": f"{C.HL_SLIPPAGE_BPS} bps per side",
        },
        "polymarket_us": {
            "maker_fee": "0% (the market-maker only posts resting orders)",
            "taker_fee": f"{C.PM_TAKER_COEF_CRYPTO * 100:.0f}% x contracts x p x (1-p) on crypto ($1.75 per 100 contracts at 50c)",
        },
        "money_movement": {
            "ach": "Free at Alpaca and Coinbase",
            "debit_card": "Avoid: card funding typically costs 1.5-2.5%",
            "wire": "Typically $10-25 each way",
        },
        "taxes": f"Short-term gains are taxed as ordinary income; simulator sets aside {C.EST_TAX_RATE * 100:.0f}% of net gains as an estimate (not tax advice)",
        "sources": C.FEE_SOURCES,
    }
