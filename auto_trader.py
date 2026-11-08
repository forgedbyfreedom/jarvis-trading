"""
Jarvis Auto-Trader
Autonomous trading engine — scans, decides, executes, manages exits.
Runs as a background thread.

Modes:
  PAPER_TRADING = True  → logs trades, tracks P&L, no real orders
  PAPER_TRADING = False → calls executors to place real orders
"""
import threading, time, logging, os
from datetime import datetime, timedelta

from config import (
    AUTO_TRADE_ENABLED, PAPER_TRADING, AUTO_TRADE_INTERVAL,
    MIN_CONFIDENCE_AUTO, MAX_OPEN_POSITIONS, MAX_POSITION_PCT,
    MAX_DAILY_LOSS_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    DCA_ENABLED, DCA_AMOUNT_USD, DCA_MIN_CONFIDENCE,
    YAHOO_TICKERS, CRYPTO_TICKERS,
    STOCKS_REGULAR_HOURS_ONLY, REENTRY_COOLDOWN_MIN, MIN_HOLD_MIN,
    ALLOW_STOCK_SHORTS, ALLOW_CRYPTO_SHORTS, TRADING_MODE, MARGIN_MIN_EQUITY,
)
import fees
import sim_account as sim
import data_sources as ds
from signal_engine import analyze, Action
import risk_manager as rm

log = logging.getLogger("jarvis.auto")

# ── STATE ──
_engine_running = False
_engine_thread = None
_engine_paused = False

def is_running() -> bool:
    return _engine_running

def is_paused() -> bool:
    return _engine_paused

def pause():
    global _engine_paused
    _engine_paused = True
    log.info("Auto-trader PAUSED")

def resume():
    global _engine_paused
    _engine_paused = False
    log.info("Auto-trader RESUMED")

def toggle() -> bool:
    pause() if _engine_paused else resume()
    return not _engine_paused

# ────────────────────────────────────────────
#  POSITION MANAGEMENT
# ────────────────────────────────────────────
def _open_position(asset, asset_type, side, qty, price, confidence, rationale=""):
    """Open a new position at a realistic fill (spread, slippage, fees)."""
    f = fees.fill(asset_type, side, price, qty)
    entry = price  # reference price; spread/slippage is booked as a cost
    if side == "buy":
        sl = rm.calc_stop_loss(entry, STOP_LOSS_PCT)
        tp = rm.calc_take_profit(entry, TAKE_PROFIT_PCT)
    else:  # short: stop above, target below
        sl = round(entry * (1 + STOP_LOSS_PCT), 4)
        tp = round(entry * (1 - TAKE_PROFIT_PCT), 4)

    pos = {
        "id":           f"{asset}_{asset_type}_{int(time.time())}",
        "asset":        asset,
        "asset_type":   asset_type,
        "side":         side,
        "qty":          qty,
        "entry_price":  entry,
        "entry_fill":   round(f["fill_price"], 6),
        "entry_costs":  f["costs"],
        "entry_costs_total": f["total_cost"],
        "venue":        f["venue"],
        "entry_time":   datetime.now().isoformat(),
        "stop_loss":    sl,
        "take_profit":  tp,
        "confidence":   confidence,
        "rationale":    rationale,
        "status":       "open",
        "pnl":          0.0,
        "paper":        PAPER_TRADING,
        "mode":         TRADING_MODE,
    }

    positions = rm.get_open_positions()
    positions.append(pos)
    _save_positions(positions)
    log.info(f"OPEN: {side.upper()} {asset} qty={qty} @ {entry} (fill {f['fill_price']:.4f}, costs ${f['total_cost']:.2f}) | SL={sl} TP={tp}")

    _execute(asset, asset_type, side, qty, entry)

    rm.log_trade({
        "asset": asset, "asset_type": asset_type, "side": side,
        "qty": qty, "price": entry, "fill_price": round(f["fill_price"], 6),
        "costs": f["costs"], "status": "open",
        "paper": PAPER_TRADING, "mode": TRADING_MODE, "confidence": confidence,
    })
    return pos


def _close_position(pos: dict, current_price: float, reason: str):
    """Close a position; P&L is booked net of every cost on both legs."""
    asset = pos["asset"]
    asset_type = pos["asset_type"]
    side = pos["side"]
    qty = pos["qty"]
    entry = pos["entry_price"]
    exit_side = "sell" if side == "buy" else "buy"

    gross = (current_price - entry) * qty if side == "buy" else (entry - current_price) * qty
    f = fees.fill(asset_type, exit_side, current_price, qty)
    costs = {}
    for leg in (pos.get("entry_costs") or {}, f["costs"]):
        for k, v in leg.items():
            costs[k] = round(costs.get(k, 0) + v, 4)
    if side == "sell" and asset_type == "stock":
        held_days = (datetime.now() - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 86400
        costs["short_borrow"] = round(fees.short_borrow_cost(entry, qty, max(held_days, 1)), 4)
    costs = {k: v for k, v in costs.items() if v}
    net = sim.record_close(f"{asset_type}:{asset.upper()}", round(gross, 4), costs)

    pos["status"] = "closed"
    pos["close_price"] = current_price
    pos["close_time"] = datetime.now().isoformat()
    pos["close_reason"] = reason
    pos["pnl"] = net

    positions = [p for p in rm.get_open_positions() if p["id"] != pos["id"]]
    _save_positions(positions)

    rm.record_pnl(net)
    log.info(f"CLOSE: {side.upper()} {asset} @ {current_price} | gross {gross:+.2f} costs {sum(costs.values()):.2f} net {net:+.2f} | {reason}")

    _execute(asset, asset_type, exit_side, qty, current_price)

    rm.log_trade({
        "asset": asset, "asset_type": asset_type, "side": exit_side,
        "qty": qty, "price": current_price, "fill_price": round(f["fill_price"], 6),
        "status": "closed", "gross_pnl": round(gross, 4), "costs": costs,
        "pnl": net, "reason": reason,
        "paper": PAPER_TRADING, "mode": TRADING_MODE,
    })
    return net


def _save_positions(positions: list):
    rm._write_json(rm.POSITIONS_PATH, positions)


def _execute(asset, asset_type, side, qty, price):
    """Place the actual order (or simulate in paper mode)."""
    if PAPER_TRADING:
        log.info(f"  [PAPER] {side.upper()} {asset} qty={qty} @ {price}")
        return {"status": "paper_executed", "asset": asset, "side": side, "qty": qty, "price": price}

    try:
        from executors import execute_stock_trade, execute_crypto_trade
        if asset_type == "stock":
            return execute_stock_trade(asset.upper(), side, qty, True)
        else:
            product = asset.upper() + "-USD"
            return execute_crypto_trade(product, side, qty, True)
    except Exception as e:
        log.error(f"  Execute error {asset}: {e}")
        return {"status": "error", "error": str(e)}


# ────────────────────────────────────────────
#  MAIN TRADING CYCLE
# ────────────────────────────────────────────
def _trading_cycle():
    """One full scan → decide → execute → manage loop."""
    if _engine_paused:
        return

    # 1. Check daily loss limit
    if not rm.can_trade(MAX_DAILY_LOSS_PCT):
        log.info("Daily loss limit reached. Skipping cycle.")
        return

    # 2. Check and manage existing positions FIRST
    _manage_positions()

    # 3. Scan for new signals
    positions = rm.get_open_positions()
    open_count = len(positions)

    if open_count >= MAX_OPEN_POSITIONS:
        log.info(f"Max positions reached ({open_count}). Skipping new entries.")
        return

    # 4. Process stock signals
    for sym in YAHOO_TICKERS:
        try:
            df = ds.fetch_stock_history(sym, days=120)
            if df.empty or len(df) < 55:
                continue
            rec = analyze(df, sym, "stock")
            _process_signal(rec, sym, "stock", open_count, positions)
        except Exception as e:
            log.debug(f"Cycle skip {sym}: {e}")
        time.sleep(0.3)

    # 5. Process crypto signals
    for cid in CRYPTO_TICKERS:
        try:
            df = ds.fetch_crypto_klines(cid, days=90)
            if df.empty or len(df) < 55:
                continue
            rec = analyze(df, cid, "crypto")
            _process_signal(rec, cid, "crypto", open_count, positions)
        except Exception as e:
            log.debug(f"Cycle skip {cid}: {e}")
        time.sleep(0.5)


def _process_signal(rec, asset, asset_type, open_count, positions):
    """Evaluate a single signal for auto-trading, with real-world constraints."""
    if rec.action == Action.HOLD:
        return
    conf = rec.confidence
    if conf < MIN_CONFIDENCE_AUTO:
        return
    if rm.has_position(asset, asset_type):
        return
    if len(rm.get_open_positions()) >= MAX_OPEN_POSITIONS:
        return

    side = rec.action.value.lower()  # "buy" or "sell"
    if side == "sell":
        if asset_type == "crypto" and not ALLOW_CRYPTO_SHORTS:
            sim.note_skip("crypto short (spot can't short)")
            return
        if asset_type == "stock" and not ALLOW_STOCK_SHORTS:
            sim.note_skip("stock short disabled")
            return
        if asset_type == "stock" and sim.equity(rm.get_open_positions()) < MARGIN_MIN_EQUITY:
            sim.note_skip(f"stock short (needs ${MARGIN_MIN_EQUITY:,.0f} margin account)")
            return
    if asset_type == "stock" and STOCKS_REGULAR_HOURS_ONLY and not fees.us_market_open():
        return  # no fills outside the regular session
    last = sim.last_exit(f"{asset_type}:{asset.upper()}")
    if last and datetime.now() - last < timedelta(minutes=REENTRY_COOLDOWN_MIN):
        return

    # Fill at the LIVE price, not the daily close the signal was computed on.
    price = _get_price(asset, asset_type)
    if not price or price <= 0:
        sim.note_skip("no live quote")
        return

    equity = sim.equity(rm.get_open_positions())
    size_usd = rm.calc_position_size(conf, MAX_POSITION_PCT, capital=equity)
    if DCA_ENABLED and side == "buy" and conf >= DCA_MIN_CONFIDENCE:
        size_usd = min(max(size_usd, DCA_AMOUNT_USD), equity * MAX_POSITION_PCT)
    qty = round(size_usd / price, 6)
    if qty <= 0:
        return

    log.info(f"  SIGNAL: {side.upper()} {asset} conf={conf:.0%} live=${price} qty={qty}")
    _open_position(asset, asset_type, side, qty, price, conf, rec.rationale)


def _manage_positions():
    """Check all open positions for stop-loss / take-profit / signal flip."""
    positions = rm.get_open_positions()
    if not positions:
        return

    for pos in list(positions):
        asset = pos["asset"]
        asset_type = pos["asset_type"]
        if asset_type == "stock" and STOCKS_REGULAR_HOURS_ONLY and not fees.us_market_open():
            continue  # can't exit a stock while the market is closed

        # Get current price
        current_price = _get_price(asset, asset_type)
        if current_price is None or current_price <= 0:
            continue

        # Check exit conditions
        exit_signal = rm.check_exit(pos, current_price)
        if exit_signal:
            log.info(f"  EXIT TRIGGERED: {asset} → {exit_signal} (price={current_price})")
            _close_position(pos, current_price, exit_signal)
            continue

        # Check if signal flipped (opposite direction with high confidence)
        _check_signal_flip(pos, asset, asset_type, current_price)


def _get_price(asset, asset_type) -> float:
    """Get current price for an asset."""
    try:
        if asset_type == "stock":
            q = ds.fetch_stock_quote(asset.upper())
            return q.get("price")
        else:
            prices = ds.fetch_crypto_prices([asset])
            return prices.get(asset)
    except Exception as e:
        log.debug(f"Price fetch {asset}: {e}")
    return None


def _check_signal_flip(pos, asset, asset_type, current_price):
    """If the signal now strongly disagrees with our position, exit."""
    try:
        if asset_type == "stock":
            df = ds.fetch_stock_history(asset, days=90)
        else:
            df = ds.fetch_crypto_klines(asset, days=60)

        if df.empty or len(df) < 55:
            return
        rec = analyze(df, asset, asset_type)

        # Only exit on strong opposite signal, and not in the first minutes
        if rec.confidence < 0.80:
            return
        held = datetime.now() - datetime.fromisoformat(pos["entry_time"])
        if held < timedelta(minutes=MIN_HOLD_MIN):
            return

        pos_side = pos["side"]
        new_action = rec.action.value.lower()

        if (pos_side == "buy" and new_action == "sell") or \
           (pos_side == "sell" and new_action == "buy"):
            log.info(f"  SIGNAL FLIP: {asset} was {pos_side}, now {new_action} @ {rec.confidence:.0%}")
            _close_position(pos, current_price, f"signal_flip_{new_action}")
    except Exception as e:
        log.debug(f"Flip check {asset}: {e}")


# ────────────────────────────────────────────
#  ENGINE LOOP
# ────────────────────────────────────────────
def _engine_loop():
    global _engine_running
    log.info("Auto-trader engine started" + (" (DRY RUN)" if PAPER_TRADING else " (LIVE)"))
    sim.load()  # creates the dry-run account (archiving old fee-free history) on first start
    _engine_running = True

    while True:
        cycle_start = time.time()
        try:
            _trading_cycle()
        except Exception as e:
            log.error(f"Trading cycle error: {e}", exc_info=True)
        _engine_running = True

        elapsed = time.time() - cycle_start
        sleep_time = max(0, AUTO_TRADE_INTERVAL - elapsed)
        log.info(f"Cycle done in {elapsed:.1f}s. Next in {sleep_time:.0f}s.")
        time.sleep(sleep_time)


def start_engine():
    """Start the auto-trader (call on app startup)."""
    global _engine_thread
    if _engine_thread and _engine_thread.is_alive():
        log.info("Auto-trader already running.")
        return
    _engine_thread = threading.Thread(target=_engine_loop, daemon=True, name="auto-trader")
    _engine_thread.start()
    log.info("Auto-trader thread started.")


def stop_engine():
    global _engine_running
    _engine_running = False
    log.info("Auto-trader stopped.")
