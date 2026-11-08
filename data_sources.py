"""
Jarvis Data Sources
Stock:  Yahoo Finance
Crypto: CoinGecko → Binance → Coinbase Exchange
"""
import logging
import pandas as pd
import requests as _rq

log = logging.getLogger("jarvis.data")

# ────────────────────────────────────────────
#  STOCK (Yahoo Finance)
# ────────────────────────────────────────────
def fetch_stock_history(ticker: str, days: int = 120) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period=f"{days}d", interval="1d", progress=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=["open","high","low","close","volume"])
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower().strip() for c in df.columns]
    required = ["open","high","low","close","volume"]
    for col in required:
        if col not in df.columns:
            return pd.DataFrame(columns=required)
    return df[required].dropna(subset=["close"]).reset_index(drop=True)


def fetch_stock_quote(symbol: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).fast_info
        return {
            "symbol": symbol,
            "price":    getattr(info, "last_price", None),
            "open":     getattr(info, "open", None),
            "high":     getattr(info, "day_high", None),
            "low":      getattr(info, "day_low", None),
            "volume":   getattr(info, "last_volume", None),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


# ────────────────────────────────────────────
#  CRYPTO
# ────────────────────────────────────────────
_COINGECKO_IDS = {
    "bitcoin":    "bitcoin",
    "ethereum":   "ethereum",
    "solana":     "solana",
    "ripple":     "ripple",
    "cardano":    "cardano",
    "dogecoin":   "dogecoin",
    "polkadot":   "polkadot",
    "chainlink":  "chainlink",
    "avalanche":  "avalanche-2",
    "polygon":    "polygon-ecosystem-token",
}

def _coingecko_ohlcv(cid: str, days: int) -> pd.DataFrame:
    """CoinGecko — works from US, no geo-block."""
    cg_id = _COINGECKO_IDS.get(cid.lower(), cid.lower())
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
    params = {"vs_currency": "usd", "days": min(days, 365), "interval": "daily"}
    headers = {"Accept": "application/json"}
    r = _rq.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    prices  = data.get("prices", [])
    volumes = data.get("total_volumes", [])

    if not prices:
        return pd.DataFrame(columns=["open","high","low","close","volume"])

    closes  = [p[1] for p in prices]
    vols    = [v[1] for v in volumes] if volumes else [0] * len(closes)
    # Use close as proxy for open/high/low (CoinGecko daily gives close+volume)
    opens   = [closes[0]] + closes[:-1]  # prev close ≈ open
    highs   = [max(o,c) for o,c in zip(opens, closes)]
    lows    = [min(o,c) for o,c in zip(opens, closes)]

    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": vols,
    })


def _binance_klines(base: str, days: int) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": base + "USDT", "interval": "1d", "limit": min(days, 1000)}
    r = _rq.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data, columns=[
        "ot","open","high","low","close","vol","ct","qv","tr","tbb","tbq","ig"
    ])
    return df[["open","high","low","close","vol"]].astype(float).rename(columns={"vol":"volume"})


def _coinbase_exchange_klines(base: str, days: int) -> pd.DataFrame:
    url = f"https://api.exchange.coinbase.com/products/{base}-USD/candles"
    params = {"granularity": "86400", "limit": min(days, 300)}
    r = _rq.get(url, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    candles = r.json()
    rows = [{"open":c[3],"high":c[2],"low":c[1],"close":c[4],"volume":c[0]} for c in candles]
    return pd.DataFrame(rows).astype(float)


def fetch_crypto_klines(cid: str, days: int = 90) -> pd.DataFrame:
    base = cid.upper()
    empty = pd.DataFrame(columns=["open","high","low","close","volume"])
    for name, fn, arg in [
        ("CoinGecko",  _coingecko_ohlcv,        cid),
        ("Binance",    _binance_klines,         base),
        ("CoinbaseEx", _coinbase_exchange_klines, base),
    ]:
        try:
            log.info(f"Trying {name} for {cid}…")
            df = fn(arg, days)
            if not df.empty:
                log.info(f"{name} OK: {len(df)} rows")
                return df
        except Exception as e:
            log.warning(f"{name} failed: {e}")
    log.error(f"All sources failed for {cid}")
    return empty


def fetch_crypto_prices(tickers: list) -> dict:
    out = {}
    try:
        ids = [_COINGECKO_IDS.get(t.lower(), t.lower()) for t in tickers]
        id_list = ",".join(ids)
        url = f"https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": id_list, "vs_currencies": "usd"}
        r = _rq.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for t in tickers:
                cg = _COINGECKO_IDS.get(t.lower(), t.lower())
                out[t] = data.get(cg, {}).get("usd")
            return out
    except Exception as e:
        log.error(f"Price fetch error: {e}")
    for t in tickers:
        out.setdefault(t, None)
    return out


# ────────────────────────────────────────────
#  SENTIMENT
# ────────────────────────────────────────────
def fetch_sentiment_snapshot() -> dict:
    try:
        r = _rq.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return {
                    "current": {
                        "value": int(data[0].get("value", 50)),
                        "label": data[0].get("value_classification", "Neutral"),
                    },
                    "week": [
                        {"value": int(d.get("value",50)), "label": d.get("value_classification","")}
                        for d in data
                    ],
                }
    except Exception as e:
        log.error(f"Sentiment: {e}")
    return {"error": "unable to fetch"}
