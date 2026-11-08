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
# TRADING_MODE: "dry_run" = simulated fills on live prices with every fee charged,
#               no money moves. "live" = real orders (needs broker keys above).
TRADING_MODE          = os.environ.get("JARVIS_TRADING_MODE", "dry_run")
PAPER_TRADING         = TRADING_MODE != "live"
AUTO_TRADE_INTERVAL   = 120

MIN_CONFIDENCE_AUTO   = 0.78
MAX_OPEN_POSITIONS    = 8
MAX_POSITION_PCT      = 0.10
MAX_DAILY_LOSS_PCT    = 0.05
STOP_LOSS_PCT         = 0.03
TAKE_PROFIT_PCT       = 0.08
TRAILING_STOP_PCT     = 0.04

# Realism guards (apply in dry run AND live)
STOCKS_REGULAR_HOURS_ONLY = True   # stale after-hours quotes created fake wins before
REENTRY_COOLDOWN_MIN  = 240        # wait 4h before re-entering the same asset
MIN_HOLD_MIN          = 30         # no signal-flip exits in the first 30 min
ALLOW_STOCK_SHORTS    = True       # only used once equity reaches MARGIN_MIN_EQUITY
MARGIN_MIN_EQUITY     = 2_000.0    # FINRA/Alpaca minimum equity for a margin account (needed to short)
ALLOW_CRYPTO_SHORTS   = False      # spot exchanges can't short

# DCA
DCA_ENABLED           = True
DCA_AMOUNT_USD        = 200
DCA_MIN_CONFIDENCE    = 0.82

# Rebalancing
REBALANCE_ENABLED     = True
REBALANCE_INTERVAL    = 3600
TARGET_ALLOCATION     = {"stocks": 0.6, "crypto": 0.4}

# ── DRY-RUN SIMULATOR ──
SIM_STARTING_CAPITAL  = 500.0      # each simulator (auto-trader, Polymarket, CopyBot) starts with this
PM_SIM_ENABLED        = True       # Polymarket US 5-min market-maker (always dry run)
COPY_SIM_ENABLED      = True       # CopyBot Hyperliquid mirror (always dry run)
EST_TAX_RATE          = 0.30       # short-term gains set-aside estimate (fed + SC), not tax advice

# ── FEES (verified Nov 2026; update here if a broker changes them) ──
STOCK_COMMISSION      = 0.0        # Alpaca stocks/ETFs
SEC_FEE_PER_MILLION   = 20.60      # SEC Section 31, sells only, effective Apr 4 2026
FINRA_TAF_PER_SHARE   = 0.000195   # FINRA Trading Activity Fee, sells only
FINRA_TAF_MAX         = 9.79
STOCK_HALF_SPREAD_BPS = 2.0        # large caps; half the bid/ask spread you cross
STOCK_SLIPPAGE_BPS    = 2.0
SHORT_BORROW_APR      = 0.0030     # easy-to-borrow stock loan fee

CRYPTO_VENUE          = "coinbase"  # "coinbase" or "alpaca"
CRYPTO_VENUES = {
    "coinbase": {"name": "Coinbase Advanced (US Intro tier)", "maker": 0.0050, "taker": 0.0090,
                 "half_spread_bps": 3.0, "slippage_bps": 3.0},
    "alpaca":   {"name": "Alpaca Crypto (tier 1)", "maker": 0.0015, "taker": 0.0025,
                 "half_spread_bps": 4.0, "slippage_bps": 3.0},
}

HL_TAKER_FEE          = 0.00045    # Hyperliquid perps base tier
HL_MAKER_FEE          = 0.00015
HL_SLIPPAGE_BPS       = 3.0

PM_TAKER_COEF_CRYPTO  = 0.07       # Polymarket US crypto taker coefficient (eff. Jul 1 2026)

FEE_SOURCES = [
    {"name": "SEC Section 31 rate FY2026", "url": "https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2"},
    {"name": "FINRA TAF", "url": "https://www.brokerage-review.com/account-fee/taf-fee.aspx"},
    {"name": "Alpaca crypto fees", "url": "https://docs.alpaca.markets/docs/crypto-fees"},
    {"name": "Coinbase Advanced fees (Sep 2026 schedule)", "url": "https://www.altrady.com/blog/crypto-trading-platforms/coinbase-advanced-trade"},
    {"name": "Polymarket US fees", "url": "https://www.rivermarkets.com/insights/polymarket-us-fees.html"},
]

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
