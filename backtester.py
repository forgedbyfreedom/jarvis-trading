"""
Backtesting Engine - replays signal engine over historical data.
"""
import numpy as np
import pandas as pd
from typing import Dict, List
import logging

from signal_engine import analyze, Action
import data_sources as ds

log = logging.getLogger("jarvis.backtest")

def run_backtest(symbol: str, asset_type: str, days: int = 365) -> Dict:
    if asset_type == "stock":
        df = ds.fetch_stock_history_long(symbol, days=days + 30)
    else:
        df = ds.fetch_crypto_klines(symbol, days=days + 30)

    if df.empty or len(df) < 60:
        return {"error": "Not enough historical data (need 60+ days)"}

    close = df["close"]
    open_ = df["open"]
    n = len(df)

    trades: List[Dict] = []
    equity = [10000.0]
    position = 0
    entry_price = 0.0
    max_equity = 10000.0
    max_drawdown = 0.0
    current_equity = 10000.0

    for i in range(60, n - 1):
        slice_df = df.iloc[:i + 1]
        try:
            rec = analyze(slice_df, symbol, asset_type)
        except Exception:
            continue

        action = rec.action
        next_open = float(open_.iloc[i + 1])
        next_close = float(close.iloc[i + 1])

        if action == Action.BUY and position == 0:
            position = 1
            entry_price = next_open
            trades.append({
                "date": str(df.index[i+1].date() if hasattr(df.index[i+1],'date') else df.index[i+1]),
                "side": "BUY", "price": next_open, "confidence": rec.confidence,
            })
        elif action == Action.SELL and position == 1:
            pnl = (next_open - entry_price) / entry_price
            current_equity *= (1 + pnl)
            trades.append({
                "date": str(df.index[i+1].date() if hasattr(df.index[i+1],'date') else df.index[i+1]),
                "side": "SELL", "price": next_open,
                "pnl_pct": round(pnl * 100, 2),
                "equity": round(current_equity, 2),
            })
            position = 0

        if position == 1:
            unrealized = (next_close - entry_price) / entry_price
            equity.append(current_equity * (1 + unrealized))
        else:
            equity.append(current_equity)

        if equity[-1] > max_equity:
            max_equity = equity[-1]
        dd = (max_equity - equity[-1]) / max_equity if max_equity > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    if position == 1:
        final_price = float(close.iloc[-1])
        pnl = (final_price - entry_price) / entry_price
        current_equity *= (1 + pnl)
        trades.append({
            "date": "END", "side": "CLOSE", "price": final_price,
            "pnl_pct": round(pnl * 100, 2), "equity": round(current_equity, 2),
        })
        equity.append(current_equity)

    closed_trades = [t for t in trades if "pnl_pct" in t]
    wins   = [t for t in closed_trades if t["pnl_pct"] > 0]
    losses = [t for t in closed_trades if t["pnl_pct"] <= 0]
    total_trades = len(closed_trades)
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss   = abs(sum(t["pnl_pct"] for t in losses))
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        profit_factor = 99.99
    else:
        profit_factor = 0.0
    total_return_pct = ((current_equity / 10000) - 1) * 100

    return {
        "symbol": symbol, "days": days,
        "total_trades": total_trades,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": profit_factor,
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "final_equity": round(current_equity, 2),
        "trades": trades,
        "equity_curve": equity,
    }
