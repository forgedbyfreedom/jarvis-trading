"""
Jarvis Auto-Trader
Autonomous trading engine — scans, decides, executes, manages exits.
Runs as a background thread.

Modes:
  PAPER_TRADING = True  → logs trades, tracks P&L, no real orders
  PAPER_TRADING = False → calls executors to place real orders
"""
import threading, time, logging, os
from datetime import datetime

from config import (
    AUTO_TRADE_ENABLED, PAPER_TRADING, AUTO_TRADE_INTERVAL,
    MIN_CONFIDENCE_AUTO, MAX_OPEN_POSITIONS, MAX_POSITION_PCT,
    MAX_DAILY_LOSS_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    DCA_ENABLED, DCA_AMOUNT_USD, DCA_MIN_CONFIDENCE,
    YAHOO_TICKERS, CRYPTO_TICKERS,
)
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
    """Open a new position."""
    entry = price
    if side == "buy":
        sl = rm.calc_stop_loss(entry, STOP_LOSS_PCT)
        tp = rm.calc_take_profit(entry, TAKE_PROFIT_PCT)
    else:
        sl = rm.calc_stop_loss(entry, STOP_LOSS_PCT)
        tp = rm.calc_take_profit(entry, TAKE_PROFIT_PCT)

    pos = {
        "id":           f"{asset}_{asset_type}_{int(time.time())}",
        "asset":        asset,
        "asset_type":   asset_type,
        "side":         side,
        "qty":          qty,
        "entry_price":  entry,
        "entry_time":   datetime.now().isoformat(),
        "stop_loss":    sl,
        "take_profit":  tp,
        "confidence":   confidence,
        "rationale":    rationale,
        "status":       "open",
        "pnl":          0.0,
        "paper":        PAPER_TRADING,
    }

    positions = rm.get_open_positions()
    positions.append(pos)
    _save_positions(positions)
    log.info(f"OPEN: {side.upper()} {asset} qty={qty} @ {entry} | SL={sl} TP={tp}")

    # Execute
    _execute(asset, asset_type, side, qty, entry)

    # Log
    rm.log_trade({
        "asset": asset, "asset_type": asset_type, "side": side,
        "qty": qty, "price": entry, "status": "open",
        "paper": PAPER_TRADING, "confidence": confidence,
    })
    return pos


def _close_position(pos: dict, current_price: float, reason: str):
    """Close an existing position."""
    asset = pos["asset"]
    asset_type = pos["asset_type"]
    side = pos["side"]
    qty = pos["qty"]
    entry = pos["entry_price"]

    # Calculate P&L
    if side == "buy":
        pnl = (current_price - entry) * qty
    else:
        pnl = (entry - current_price) * qty
    pnl = round(pnl, 4)

    pos["status"] = "closed"
    pos["close_price"] = current_price
    pos["close_time"] = datetime.now().isoformat()
    pos["close_reason"] = reason
    pos["pnl"] = pnl

    # Update in positions file (mark closed)
    positions = rm.get_open_positions()
    positions = [p for p in positions if p["id"] != pos["id"]]
    _save_positions(positions)

    # Record P&L
    rm.record_pnl(pnl)

    log.info(f"CLOSE: {side.upper()} {asset} @ {current_price} | P&L={pnl:+.2f} | {reason}")

    _execute(asset, asset_type, "sell" if side == "buy" else "buy", qty, current_price)

    rm.log_trade({
        "asset": asset, "asset_type": asset_type,
        "side": "sell" if side == "buy" else "buy",
        "qty": qty, "price": current_price,
        "status": "closed", "pnl": pnl, "reason": reason,
        "paper": PAPER_TRADING,
    })
    return pnl


def _save_positions(positions: list):
    import json
    os.makedirs("data", exist_ok=True)
    with open("data/positions.json", "w") as f:
        json.dump(positions, f, indent=2)


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
    """Evaluate a single signal for auto-trading."""
    if rec.action == Action.HOLD:
        return

    conf = rec.confidence
    if conf < MIN_CONFIDENCE_AUTO:
        return

    # Already in this position?
    if rm.has_position(asset, asset_type):
        return

    # Max positions?
    if open_count >= MAX_OPEN_POSITIONS:
        return

    side = rec.action.value.lower()  # "buy" or "sell"

    # Position sizing
    size_usd = rm.calc_position_size(conf, MAX_POSITION_PCT)

    # DCA check
    if DCA_ENABLED and side == "buy":
        if conf >= DCA_MIN_CONFIDENCE:
            size_usd = max(size_usd, DCA_AMOUNT_USD)
            log.info(f"  DCA boost for {asset}: ${size_usd}")

    qty = round(size_usd / rec.price, 6) if rec.price > 0 else 0
    if qty <= 0:
        return

    log.info(f"  SIGNAL: {side.upper()} {asset} conf={conf:.0%} price=${rec.price} qty={qty}")
    _open_position(asset, asset_type, side, qty, rec.price, conf, rec.rationale)


def _manage_positions():
    """Check all open positions for stop-loss / take-profit / signal flip."""
    positions = rm.get_open_positions()
    if not positions:
        return

    for pos in list(positions):
        asset = pos["asset"]
        asset_type = pos["asset_type"]

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

        # Only exit on strong opposite signal
        if rec.confidence < 0.80:
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
    log.info("Auto-trader engine started" + (" (PAPER)" if PAPER_TRADING else " (LIVE)"))
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
