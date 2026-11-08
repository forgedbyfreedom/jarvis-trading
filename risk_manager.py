"""
Jarvis Risk Manager
Enforces position limits, daily loss caps, and position sizing.
"""
import json, os, logging, time
from datetime import datetime

log = logging.getLogger("jarvis.risk")

def _read_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _today_str():
    return datetime.now().strftime("%Y-%m-%d")

# ────────────────────────────────────────────
#  DAILY P&L TRACKING
# ────────────────────────────────────────────
def get_daily_pnl() -> float:
    data = _read_json("data/daily_pnl.json", {"date": "", "pnl": 0.0})
    if data.get("date") != _today_str():
        return 0.0
    return data.get("pnl", 0.0)

def record_pnl(amount: float):
    data = _read_json("data/daily_pnl.json", {"date": "", "pnl": 0.0})
    if data.get("date") != _today_str():
        data = {"date": _today_str(), "pnl": 0.0}
    data["pnl"] = round(data.get("pnl", 0) + amount, 4)
    _write_json("data/daily_pnl.json", data)
    log.info(f"Daily P&L updated: {data['pnl']}")

def can_trade(max_daily_loss_pct: float) -> bool:
    """Check if we're within daily loss limit."""
    pnl = get_daily_pnl()
    # Negative pnl means loss
    if pnl < 0:
        # Estimate capital as 10000 baseline (adjust via portfolio)
        daily_loss_pct = abs(pnl) / 10000.0
        if daily_loss_pct >= max_daily_loss_pct:
            log.warning(f"Daily loss limit hit: {daily_loss_pct:.2%} >= {max_daily_loss_pct:.2%}")
            return False
    return True

# ────────────────────────────────────────────
#  POSITION COUNT
# ────────────────────────────────────────────
def get_open_positions() -> list:
    return _read_json("data/positions.json", [])

def count_open_positions() -> int:
    return len(get_open_positions())

def has_position(asset: str, asset_type: str) -> bool:
    for p in get_open_positions():
        if p["asset"].upper() == asset.upper() and p["asset_type"] == asset_type:
            return True
    return False

# ────────────────────────────────────────────
#  POSITION SIZING
# ────────────────────────────────────────────
def calc_position_size(confidence: float, max_position_pct: float,
                       capital: float = 10000.0) -> float:
    """
    Scale position size by confidence.
    Higher confidence → larger position.
    Returns dollar amount.
    """
    base = capital * max_position_pct
    # Scale: 0.78 confidence → 50% of max, 1.0 → 100% of max
    scale = max(0.5, (confidence - 0.70) / 0.30)
    size = base * min(scale, 1.0)
    return round(size, 2)

# ────────────────────────────────────────────
#  STOP LOSS / TAKE PROFIT
# ────────────────────────────────────────────
def calc_stop_loss(entry_price: float, sl_pct: float) -> float:
    return round(entry_price * (1 - sl_pct), 4)

def calc_take_profit(entry_price: float, tp_pct: float) -> float:
    return round(entry_price * (1 + tp_pct), 4)

def check_exit(position: dict, current_price: float) -> str:
    """
    Returns 'stop_loss', 'take_profit', or '' if no exit needed.
    """
    side = position.get("side", "buy")
    entry = position.get("entry_price", 0)
    if entry <= 0:
        return ""

    if side == "buy":
        sl = position.get("stop_loss", entry * 0.97)
        tp = position.get("take_profit", entry * 1.08)
        if current_price <= sl:
            return "stop_loss"
        if current_price >= tp:
            return "take_profit"
    elif side == "sell":
        sl = position.get("stop_loss", entry * 1.03)
        tp = position.get("take_profit", entry * 0.92)
        if current_price >= sl:
            return "stop_loss"
        if current_price <= tp:
            return "take_profit"
    return ""

# ────────────────────────────────────────────
#  TRADE HISTORY
# ────────────────────────────────────────────
def log_trade(trade: dict):
    history = _read_json("data/trade_history.json", [])
    trade["timestamp"] = datetime.now().isoformat()
    history.insert(0, trade)
    # Keep last 500
    history = history[:500]
    _write_json("data/trade_history.json", history)
    log.info(f"Trade logged: {trade.get('asset')} {trade.get('side')} {trade.get('qty')} @ {trade.get('price')}")

def get_trade_history(limit: int = 50) -> list:
    return _read_json("data/trade_history.json", [])[:limit]

def get_win_rate() -> dict:
    history = _read_json("data/trade_history.json", [])
    closed = [t for t in history if t.get("status") == "closed"]
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    total_pnl = sum(t.get("pnl", 0) for t in closed)
    return {
        "total_trades": len(history),
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
    }
