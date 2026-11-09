"""
Jarvis Command Center — FastAPI Server
Run: uvicorn app:app --reload --port 8080
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import threading, time, logging, os, json

from config import (
    YAHOO_TICKERS, CRYPTO_TICKERS, SCAN_INTERVAL_SEC,
    TARGET_CHECK_SEC, QUICK_LINKS, SEARCH, DATA_DIR, WATCHLIST_FILE,
    MIN_CONFIDENCE,
    AUTO_TRADE_ENABLED, PAPER_TRADING, TRADING_MODE,
    PM_SIM_ENABLED, COPY_SIM_ENABLED,
)
import data_sources as ds
from signal_engine import analyze, Recommendation, Action
from executors import (
    execute_stock_trade, execute_crypto_trade,
    get_stock_portfolio, get_crypto_balances,
    get_funding_links, get_audit_log,
)
import alerts as alert_mod
import price_targets as pt
from backtester import run_backtest
import auto_trader as at
import risk_manager as rm
import advisory as adv
import fees
import sim_account
import pm_sim
import copy_sim
import hub
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO,
                    format="%(name)s | %(levelname)s | %(message)s")
log = logging.getLogger("jarvis")

app = FastAPI(title="Jarvis Command Center", version="3.0.0")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# ── SCAN STATE ──
_scan_lock = threading.Lock()
_last_scan = {"stocks": {}, "crypto": {}, "ts": 0}
_alerted: set = set()

def _run_scan():
    global _last_scan, _alerted
    with _scan_lock:
        out = {"stocks": {}, "crypto": {}, "ts": time.time()}
        _alerted.clear()
        for sym in YAHOO_TICKERS:
            try:
                df = ds.fetch_stock_history(sym, days=120)
                if not df.empty and len(df) >= 55:
                    rec = analyze(df, sym, "stock")
                    out["stocks"][sym] = {
                        "asset": rec.asset, "asset_type": rec.asset_type,
                        "price": rec.price, "action": rec.action.value,
                        "confidence": rec.confidence,
                        "rationale": rec.rationale,
                        "indicators": rec.indicators,
                    }
                    if rec.action != Action.HOLD and rec.confidence >= MIN_CONFIDENCE:
                        key = f"{sym}_{rec.action.value}_{int(time.time()//3600)}"
                        if key not in _alerted:
                            _alerted.add(key)
                            alert_mod.alert_recommendation(
                                sym, "stock", rec.action.value,
                                rec.price, rec.confidence, rec.rationale)
            except Exception as e:
                log.error(f"Scan error {sym}: {e}")
            time.sleep(0.3)
        for cid in CRYPTO_TICKERS:
            try:
                df = ds.fetch_crypto_klines(cid, days=90)
                if not df.empty and len(df) >= 55:
                    rec = analyze(df, cid, "crypto")
                    out["crypto"][cid] = {
                        "asset": rec.asset, "asset_type": rec.asset_type,
                        "price": rec.price, "action": rec.action.value,
                        "confidence": rec.confidence,
                        "rationale": rec.rationale,
                        "indicators": rec.indicators,
                    }
                    if rec.action != Action.HOLD and rec.confidence >= MIN_CONFIDENCE:
                        key = f"{cid}_{rec.action.value}_{int(time.time()//3600)}"
                        if key not in _alerted:
                            _alerted.add(key)
                            alert_mod.alert_recommendation(
                                cid, "crypto", rec.action.value,
                                rec.price, rec.confidence, rec.rationale)
            except Exception as e:
                log.error(f"Scan error {cid}: {e}")
            time.sleep(0.5)
        _last_scan = out
    log.info(f"Scan complete: {len(out['stocks'])} stocks, {len(out['crypto'])} crypto")

def _background_scan_loop():
    while True:
        try:
            _run_scan()
        except Exception as e:
            log.error(f"Scan loop error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)

# ── LIFECYCLE ──
@app.on_event("startup")
def startup():
    os.makedirs(DATA_DIR, exist_ok=True)
    t = threading.Thread(target=_background_scan_loop, daemon=True)
    t.start()
    pt.start_target_checker(TARGET_CHECK_SEC)
    sim_account.load()
    if AUTO_TRADE_ENABLED:
        at.start_engine()
    if PM_SIM_ENABLED:
        pm_sim.start()
    if COPY_SIM_ENABLED:
        copy_sim.start()
    hub.start()
    log.info("Jarvis Command Center v4 online. Auto-trade: " +
             ("ON (DRY RUN)" if PAPER_TRADING else "ON (LIVE)" if AUTO_TRADE_ENABLED else "OFF"))

# ── PAGES ──
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(name="index.html", context={}, request=request)

# ── SCAN ──
@app.get("/api/scan")
def api_scan():
    if not _last_scan.get("stocks"):
        _run_scan()
    return _last_scan

@app.get("/api/sentiment")
def api_sentiment():
    return ds.fetch_sentiment_snapshot()

@app.get("/api/quote/{symbol}")
def api_quote(symbol: str):
    return ds.fetch_stock_quote(symbol.upper())

@app.get("/api/crypto-prices")
def api_crypto_prices():
    return ds.fetch_crypto_prices(CRYPTO_TICKERS)

# ── EXECUTE (manual) ──
class TradeRequest(BaseModel):
    asset:          str
    asset_type:     str
    side:           str
    qty:            float
    user_confirmed: bool

@app.post("/api/execute")
def api_execute(req: TradeRequest):
    if req.asset_type == "stock":
        return execute_stock_trade(req.asset.upper(), req.side.lower(), req.qty, req.user_confirmed)
    elif req.asset_type == "crypto":
        product = req.asset.upper() + "-USD"
        return execute_crypto_trade(product, req.side.lower(), req.qty, req.user_confirmed)
    raise HTTPException(400, "asset_type must be 'stock' or 'crypto'")

# ── PORTFOLIO ──
@app.get("/api/portfolio/stocks")
def api_stock_portfolio():
    return get_stock_portfolio()

@app.get("/api/portfolio/crypto")
def api_crypto_portfolio():
    return get_crypto_balances()

# ── FUNDING ──
@app.get("/api/funding")
def api_funding():
    return get_funding_links()

# ── AUDIT ──
@app.get("/api/audit")
def api_audit():
    return get_audit_log()

# ── PRICE TARGETS ──
class TargetRequest(BaseModel):
    asset:        str
    asset_type:   str
    entry_price:  float
    target_price: float
    direction:    str = "above"

@app.post("/api/targets")
def api_add_target(req: TargetRequest):
    return pt.add_target(req.asset, req.asset_type, req.entry_price, req.target_price, req.direction)

@app.get("/api/targets")
def api_list_targets(active_only: bool = False):
    return pt.list_targets(active_only)

@app.delete("/api/targets/{target_id}")
def api_remove_target(target_id: str):
    if not pt.remove_target(target_id):
        raise HTTPException(404, "Target not found")
    return {"status": "deleted"}

# ── WATCHLIST ──
def _load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r") as f: return json.load(f)
    except Exception: return []

def _save_watchlist(wl):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WATCHLIST_FILE, "w") as f: json.dump(wl, f, indent=2)

class WatchlistItem(BaseModel):
    asset: str
    asset_type: str

@app.get("/api/watchlist")
def api_get_watchlist():
    return _load_watchlist()

@app.post("/api/watchlist")
def api_add_watchlist(item: WatchlistItem):
    wl = _load_watchlist()
    if not any(w["asset"].upper() == item.asset.upper() and w["asset_type"] == item.asset_type for w in wl):
        wl.append({"asset": item.asset, "asset_type": item.asset_type})
        _save_watchlist(wl)
    return wl

@app.delete("/api/watchlist")
def api_remove_watchlist(asset: str, asset_type: str):
    wl = [w for w in _load_watchlist() if not (w["asset"].upper() == asset.upper() and w["asset_type"] == asset_type)]
    _save_watchlist(wl)
    return wl

@app.put("/api/watchlist/reorder")
def api_reorder_watchlist(payload: list):
    _save_watchlist(payload)
    return {"status": "ok"}

# ── BACKTEST ──
@app.get("/api/backtest/{symbol}")
def api_backtest(symbol: str, asset_type: str = "stock", days: int = 365):
    return run_backtest(symbol, asset_type, days)

# ── LINKS ──
@app.get("/api/links")
def api_links():
    return QUICK_LINKS

@app.get("/api/search-config")
def api_search_config():
    return SEARCH

# ═══════════════════════════════════════════
#  NEW: AUTO-TRADER ENDPOINTS
# ═══════════════════════════════════════════
@app.get("/api/auto-trade/status")
def api_at_status():
    return {
        "running":      at.is_running(),
        "paused":       at.is_paused(),
        "paper_mode":   PAPER_TRADING,
        "trading_mode": TRADING_MODE,
        "open_positions": len(rm.get_open_positions()),
        "daily_pnl":    round(rm.get_daily_pnl(), 2),
        "win_stats":    rm.get_win_rate(),
    }

@app.post("/api/auto-trade/pause")
def api_at_pause():
    at.pause()
    return {"status": "paused"}

@app.post("/api/auto-trade/resume")
def api_at_resume():
    at.resume()
    return {"status": "resumed"}

@app.get("/api/auto-trade/positions")
def api_at_positions():
    return rm.get_open_positions()

@app.get("/api/auto-trade/history")
def api_at_history(limit: int = 50):
    return rm.get_trade_history(limit)

# ── CLOSE A POSITION ──
@app.post("/api/auto-trade/close/{pos_id}")
def api_at_close(pos_id: str):
    positions = rm.get_open_positions()
    pos = next((p for p in positions if p["id"] == pos_id), None)
    if not pos:
        raise HTTPException(404, "Position not found")
    # Get current price
    try:
        if pos["asset_type"] == "stock":
            price = ds.fetch_stock_quote(pos["asset"].upper()).get("price", pos["entry_price"])
        else:
            prices = ds.fetch_crypto_prices([pos["asset"]])
            price = prices.get(pos["asset"], pos["entry_price"])
    except Exception:
        price = pos["entry_price"]
    pnl = at._close_position(pos, float(price or pos["entry_price"]), "manual_close")
    return {"status": "closed", "pnl": pnl}

# ═══════════════════════════════════════════
#  NEW: ADVISORY ENDPOINT
# ═══════════════════════════════════════════
@app.get("/api/advisory")
def api_advisory():
    scan = _last_scan if _last_scan.get("stocks") else {"stocks": {}, "crypto": {}}
    if not scan.get("stocks"):
        _run_scan()
        scan = _last_scan
    try:
        sentiment = ds.fetch_sentiment_snapshot()
    except Exception:
        sentiment = {}
    try:
        win_stats = rm.get_win_rate()
    except Exception:
        win_stats = {}
    report = adv.generate_advisory(scan, {}, sentiment, win_stats)
    return report

# ═══════════════════════════════════════════
#  NEW: PORTFOLIO HEALTH
# ═══════════════════════════════════════════
@app.get("/api/portfolio/health")
def api_pf_health():
    positions = rm.get_open_positions()
    open_count = len(positions)
    daily_pnl = round(rm.get_daily_pnl(), 2)
    win_stats = rm.get_win_rate()

    # Allocation from open positions
    stock_value = sum(p.get("qty",0) * p.get("entry_price",0) for p in positions if p.get("asset_type") == "stock")
    crypto_value = sum(p.get("qty",0) * p.get("entry_price",0) for p in positions if p.get("asset_type") == "crypto")
    total = stock_value + crypto_value

    return {
        "open_positions": open_count,
        "max_positions":  8,
        "daily_pnl":      daily_pnl,
        "total_pnl":      round(win_stats.get("total_pnl", 0), 2),
        "win_rate":       win_stats.get("win_rate", 0),
        "total_trades":   win_stats.get("total_trades", 0),
        "paper_trading":  PAPER_TRADING,
        "allocation": {
            "stocks":   round(stock_value / total * 100, 1) if total > 0 else 50,
            "crypto":   round(crypto_value / total * 100, 1) if total > 0 else 50,
            "stock_value":  round(stock_value, 2),
            "crypto_value": round(crypto_value, 2),
        },
        "risk_limits": {
            "max_daily_loss_pct": 5,
            "stop_loss_pct": 3,
            "take_profit_pct": 8,
            "min_confidence_auto": 78,
        },
    }

# ──────────────────────────────────────────────────────────
#  FORGED BY FREEDOM — RAG CHAT
# ──────────────────────────────────────────────────────────
import chat as rag_chat

class ChatRequest(BaseModel):
    message: str
    history: list = []  # future: multi-turn

@app.post("/api/chat")
def api_chat(req: ChatRequest):
    """RAG-powered chat. Queries local ChromaDB, answers via Ollama."""
    if not req.message.strip():
        raise HTTPException(400, "message is empty")
    try:
        return rag_chat.chat(req.message)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        log.error(f"Chat error: {e}")
        raise HTTPException(500, f"Chat engine error: {e}")

@app.get("/api/chat/health")
def api_chat_health():
    """Check ChromaDB + Ollama status."""
    return rag_chat.health_check()



# ═══════════════════════════════════════════
#  DRY RUN: fees, simulated accounts, panels
# ═══════════════════════════════════════════
@app.get("/api/fees")
def api_fees():
    return fees.schedule()

@app.get("/api/sim/summary")
def api_sim_summary():
    return sim_account.summary(rm.get_open_positions())

@app.get("/api/sim/legacy")
def api_sim_legacy():
    return sim_account.rescore_legacy() or {}

@app.post("/api/sim/reset")
def api_sim_reset():
    """Start a fresh dry run. Old history is archived to data/archive, not deleted."""
    sim_account.start_fresh(archive_old=True)
    return sim_account.summary([])

@app.get("/api/pm/summary")
def api_pm_summary():
    return pm_sim.summary()

@app.post("/api/pm/reset")
def api_pm_reset():
    pm_sim.reset()
    return {"status": "reset"}

@app.get("/api/copy/summary")
def api_copy_summary():
    return copy_sim.summary()

@app.post("/api/copy/run")
def api_copy_run():
    threading.Thread(target=copy_sim.run_cycle, daemon=True).start()
    return {"status": "started"}

@app.post("/api/copy/reset")
def api_copy_reset():
    copy_sim.reset()
    return {"status": "reset"}


# ═══════════════════════════════════════════
#  HUB: link tiles, tile status, AI Coach, peptide research
# ═══════════════════════════════════════════
@app.get("/api/hub/links")
def api_hub_links():
    return hub.links()

class LinkUpdate(BaseModel):
    url: str
    title: Optional[str] = None

@app.put("/api/hub/links/{key}")
def api_hub_set_link(key: str, body: LinkUpdate):
    if not body.url.startswith(("https://", "http://", "mailto:")):
        raise HTTPException(400, "URL must start with https://")
    return hub.set_link(key, body.url, body.title)

@app.get("/api/hub/status")
def api_hub_status():
    out = {}
    try: out["sim"] = sim_account.summary(rm.get_open_positions())
    except Exception as e: out["sim"] = {"error": str(e)}
    try: out["pm"] = {k: v for k, v in pm_sim.summary().items() if k in ("equity", "totals", "running")}
    except Exception as e: out["pm"] = {"error": str(e)}
    try:
        c = copy_sim.summary(); out["copy"] = {"equity": c["equity"], "pnl_total": c["pnl_total"]}
    except Exception as e: out["copy"] = {"error": str(e)}
    scan = _last_scan or {}
    out["stocks"] = {"buy": sum(1 for v in scan.get("stocks", {}).values() if v.get("action") == "BUY"),
                     "sell": sum(1 for v in scan.get("stocks", {}).values() if v.get("action") == "SELL"),
                     "n": len(scan.get("stocks", {}))}
    out["crypto"] = {"buy": sum(1 for v in scan.get("crypto", {}).values() if v.get("action") == "BUY"),
                     "sell": sum(1 for v in scan.get("crypto", {}).values() if v.get("action") == "SELL"),
                     "n": len(scan.get("crypto", {}))}
    try:
        p = json.load(open(hub.PEPTIDE_FILE, encoding="utf-8"))
        out["peptides"] = {"papers": len(p.get("papers", [])), "refreshed": p.get("refreshed")}
    except Exception:
        out["peptides"] = {"papers": None}
    return out

@app.get("/api/hub/ai-health")
def api_hub_ai_health():
    return hub.ai_health()

class CoachReq(BaseModel):
    question: str
    history: list = []

@app.post("/api/coach")
def api_coach(req: CoachReq):
    if not req.question.strip():
        raise HTTPException(400, "Ask a question")
    try:
        return hub.coach_ask(req.question, req.history)
    except Exception as e:
        raise HTTPException(502, f"AI Coach unavailable: {e}")

@app.get("/api/peptides")
def api_peptides(refresh: bool = False):
    return hub.peptides(force=refresh)
