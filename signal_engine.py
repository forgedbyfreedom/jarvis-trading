"""
Jarvis Signal Engine - BUY / SELL / HOLD recommendations.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from typing import List
import logging

from config import (
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BB_PERIOD, BB_STD, SMA_SHORT, SMA_LONG, MIN_CONFIDENCE,
)

log = logging.getLogger("jarvis.signal")

class Action(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

@dataclass
class Recommendation:
    asset:      str
    asset_type: str
    price:      float
    action:     Action
    confidence: float  # 0-1
    rationale:  List[str] = field(default_factory=list)
    indicators: dict       = field(default_factory=dict)

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _macd(series: pd.Series):
    ema_f = series.ewm(span=MACD_FAST).mean()
    ema_s = series.ewm(span=MACD_SLOW).mean()
    line  = ema_f - ema_s
    sig   = line.ewm(span=MACD_SIGNAL).mean()
    return line, sig, line - sig

def _bollinger(series: pd.Series, period: int = 20, stds: float = 2):
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    return mid + stds*std, mid, mid - stds*std

def analyze(df: pd.DataFrame, symbol: str, asset_type: str) -> Recommendation:
    close  = df["close"]
    volume = df["volume"]

    rsi = _rsi(close, RSI_PERIOD).iloc[-1]
    macd_l, macd_s, macd_h = _macd(close)
    hist_last = macd_h.iloc[-1]
    hist_prev = macd_h.iloc[-2]

    bb_u, bb_m, bb_l = _bollinger(close, BB_PERIOD, BB_STD)
    price = float(close.iloc[-1])
    bb_pos = (price - bb_l.iloc[-1]) / max(bb_u.iloc[-1] - bb_l.iloc[-1], 1e-9)

    sma_s = close.rolling(SMA_SHORT).mean().iloc[-1]
    sma_l = close.rolling(SMA_LONG).mean().iloc[-1]

    vol_ratio = (volume.tail(5).mean() / volume.tail(20).mean()
                 if volume.tail(20).mean() > 0 else 1.0)

    rationale: List[str] = []
    bull = 0
    bear = 0

    if rsi < 30:
        bull += 15; rationale.append(f"RSI {rsi:.1f} - oversold, bounce potential")
    elif rsi > 70:
        bear += 15; rationale.append(f"RSI {rsi:.1f} - overbought, pullback risk")
    else:
        rationale.append(f"RSI {rsi:.1f} - neutral")

    if hist_last > 0 and hist_prev <= 0:
        bull += 20; rationale.append("MACD bullish crossover - momentum turning up")
    elif hist_last < 0 and hist_prev >= 0:
        bear += 20; rationale.append("MACD bearish crossover - momentum turning down")
    elif hist_last > 0:
        bull += 5;  rationale.append("MACD positive - mild bullish bias")
    else:
        bear += 5;  rationale.append("MACD negative - mild bearish bias")

    if bb_pos < 0.1:
        bull += 15; rationale.append("At lower Bollinger Band - stretched down")
    elif bb_pos > 0.9:
        bear += 15; rationale.append("At upper Bollinger Band - stretched up")
    else:
        rationale.append(f"Bollinger position {bb_pos:.2f} - mid-range")

    if price > sma_s > sma_l:
        bull += 20; rationale.append(f"Price > SMA{SMA_SHORT} > SMA{SMA_LONG} - uptrend")
    elif price < sma_s < sma_l:
        bear += 20; rationale.append(f"Price < SMA{SMA_SHORT} < SMA{SMA_LONG} - downtrend")
    else:
        rationale.append("Mixed MA picture")

    if vol_ratio > 1.5:
        if bull > bear:
            bull += 10; rationale.append(f"Volume {vol_ratio:.1f}x avg - confirms up-move")
        else:
            bear += 10; rationale.append(f"Volume {vol_ratio:.1f}x avg - confirms down-move")
    else:
        rationale.append(f"Volume {vol_ratio:.1f}x avg - no strong signal")

    # Confidence = strength of the net case, not each side's share of the points.
    # (A share-based score called a lone "mild MACD" point 100% certain.)
    # Max one-sided score is 80; a net of 60+ is treated as full conviction.
    net = bull - bear
    conf = int(round(50 + 50 * min(abs(net) / 60, 1.0)))
    if net > 0 and conf / 100 >= MIN_CONFIDENCE:
        action = Action.BUY
    elif net < 0 and conf / 100 >= MIN_CONFIDENCE:
        action = Action.SELL
    else:
        action = Action.HOLD

    indicators = {
        "rsi": round(float(rsi),2), "macd_hist": round(float(hist_last),4),
        "bb_position": round(float(bb_pos),3),
        "sma_20": round(float(sma_s),2), "sma_50": round(float(sma_l),2),
        "vol_ratio": round(float(vol_ratio),2), "price": round(price,2),
    }

    return Recommendation(
        asset=symbol, asset_type=asset_type, price=round(price,2),
        action=action, confidence=round(conf / 100, 2), rationale=rationale, indicators=indicators,
    )
