"""
J.A.R.V.I.S. Trading — Global Configuration
"""
import os

# ── PATHS ──
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
DATA_DIR           = os.path.join(BASE_DIR, "data")
WATCHLIST_FILE     = os.path.join(DATA_DIR, "watchlist.json")
POSITIONS_FILE     = os.path.join(DATA_DIR, "positions.json")
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.json")
DAILY_PNL_FILE     = os.path.join(DATA_DIR, "daily_pnl.json")
AUDIT_FILE         = os.path.join(DATA_DIR, "audit_log.json")
TARGETS_FILE       = os.path.join(DATA_DIR, "price_targets.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ── TICKERS ──
YAHOO_TICKERS = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","NFLX",
    "JPM","V","JNJ","XOM","UNH","HD","MA","CSCO","PEP",
    "TMO","AVGO","AMD","QCOM","CRM","ORCL","INTC","BA","DIS","PYPL",
]

CRYPTO_TICKERS = [
    "bitcoin","ethereum","solana","ripple","cardano",
    "dogecoin","chainlink","polkadot","avalanche","polygon",
]

# ── INDICATOR SETTINGS ──
RSI_PERIOD     = 14
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
BB_PERIOD      = 20
BB_STD         = 2
SMA_SHORT      = 20
SMA_LONG       = 50

# ── SCAN ──
SCAN_INTERVAL_SEC = 300       # 5 min
MIN_CONFIDENCE    = 0.70
TARGET_CHECK_SEC  = 60

# ── BROKER API KEYS ──
ALPACA_API_KEY     = ""
ALPACA_API_SECRET  = ""
ALPACA_BASE_URL    = "https://paper-api.alpaca.markets"

COINBASE_API_KEY     = ""
COINBASE_API_SECRET  = ""
COINBASE_PASSPHRASE  = ""

# ── TRADING RULES ──
REQUIRE_CONFIRM = True

# ── AUTO-TRADING ENGINE ──
AUTO_TRADE_ENABLED    = True
PAPER_TRADING         = True
AUTO_TRADE_INTERVAL   = 120

MIN_CONFIDENCE_AUTO   = 0.78
MAX_OPEN_POSITIONS    = 8
MAX_POSITION_PCT      = 0.10
MAX_DAILY_LOSS_PCT    = 0.05
STOP_LOSS_PCT         = 0.03
TAKE_PROFIT_PCT       = 0.08
TRAILING_STOP_PCT     = 0.04

# DCA
DCA_ENABLED           = True
DCA_AMOUNT_USD        = 200
DCA_MIN_CONFIDENCE    = 0.82

# Rebalancing
REBALANCE_ENABLED     = True
REBALANCE_INTERVAL    = 3600
TARGET_ALLOCATION     = {"stocks": 0.6, "crypto": 0.4}

# ── NOTIFICATIONS ──
SLACK_WEBHOOK_URL = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID   = ""

# ── LINKS ──
QUICK_LINKS = [
    {"name": "Alpaca (Stocks)",   "url": "https://alpaca.markets"},
    {"name": "Coinbase",          "url": "https://coinbase.com"},
    {"name": "Kraken",            "url": "https://kraken.com"},
    {"name": "Binance",           "url": "https://binance.com"},
    {"name": "TradingView",       "url": "https://tradingview.com"},
    {"name": "CoinMarketCap",     "url": "https://coinmarketcap.com"},
    {"name": "F&G Index",         "url": "https://alternative.me/crypto/fear-and-greed-index/"},
    {"name": "Yahoo Finance",     "url": "https://finance.yahoo.com"},
    {"name": "Bloomberg",         "url": "https://bloomberg.com"},
    {"name": "Reuters Markets",   "url": "https://reuters.com/markets"},
]

SEARCH = {
    "jarvis":  "Search ticker, asset, or strategy...",
    "google":  "Web search...",
}
