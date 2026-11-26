"""
Jarvis Command Center — FastAPI Server
Run: uvicorn app:app --reload --port 8080
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, Response
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

# ── Remote access guard (phone via Twingate) ─────────────────────────────────
# Requests from this PC pass straight through. Anything else (Twingate / network) must unlock with the PIN
# kept in data/athena_pin.txt; success sets a signed cookie good for 30 days.
import hashlib, hmac, secrets
from fastapi.responses import RedirectResponse
_PIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "athena_pin.txt")
_LOCAL = {"127.0.0.1", "::1", "localhost"}


def _athena_pin():
    try:
        return open(_PIN_FILE, encoding="utf-8").read().strip()
    except OSError:
        os.makedirs(os.path.dirname(_PIN_FILE), exist_ok=True)
        pin = f"{secrets.randbelow(10**6):06d}"
        open(_PIN_FILE, "w", encoding="utf-8").write(pin)
        return pin


def _pin_token():
    return hmac.new(_athena_pin().encode(), b"athena-remote", hashlib.sha256).hexdigest()


_PIN_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Athena">
<link rel="apple-touch-icon" href="/static/athena-180.png"><title>Athena</title>
<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#05070d;color:#e6edf7;font-family:system-ui}
form{text-align:center}img{width:96px;border-radius:22px}h1{letter-spacing:.2em;font-size:18px;color:#d4a93a}
input{font-size:28px;letter-spacing:.4em;text-align:center;width:220px;padding:12px;border-radius:12px;border:1px solid #2a3550;background:#0a1222;color:#fff}
button{display:block;margin:14px auto 0;padding:12px 40px;border:0;border-radius:12px;background:linear-gradient(90deg,#ff3d3d,#ff8a00);color:#fff;font-weight:800;font-size:16px}
p{color:#ff6b6b;min-height:1em}</style></head><body><form method="post" action="/athena-unlock">
<img src="/static/athena-180.png" alt=""><h1>ATHENA</h1><input name="pin" type="password" inputmode="numeric" autocomplete="one-time-code" autofocus maxlength="12">
<p>%ERR%</p><button>Unlock</button></form></body></html>"""


@app.middleware("http")
async def _remote_guard(request: Request, call_next):
    host = request.client.host if request.client else ""
    path = request.url.path
    if host in _LOCAL or path in ("/athena-unlock",) or path.startswith("/static/athena-"):
        return await call_next(request)
    if hmac.compare_digest(request.cookies.get("athena", ""), _pin_token()):
        return await call_next(request)
    if path.startswith("/api/"):
        return Response(status_code=401, content='{"detail":"locked"}', media_type="application/json")
    return HTMLResponse(_PIN_PAGE.replace("%ERR%", ""), status_code=401)


_unlock_fails = {}


@app.post("/athena-unlock")
async def athena_unlock(request: Request):
    host = request.client.host if request.client else "?"
    n, t0 = _unlock_fails.get(host, (0, 0))
    if n >= 5 and time.time() - t0 < 900:
        return HTMLResponse(_PIN_PAGE.replace("%ERR%", "Too many tries — wait 15 minutes"), status_code=429)
    form = (await request.body()).decode()
    pin = dict(x.split("=", 1) for x in form.split("&") if "=" in x).get("pin", "")
    if hmac.compare_digest(pin, _athena_pin()):
        _unlock_fails.pop(host, None)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("athena", _pin_token(), max_age=30 * 86400, httponly=True, samesite="lax")
        return r
    _unlock_fails[host] = (n + 1, time.time() if n == 0 else t0)
    return HTMLResponse(_PIN_PAGE.replace("%ERR%", "Wrong PIN"), status_code=401)
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
    import fbf_clients
    fbf_clients.start()
    hub.start_keep_warm()
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


# ── CopyBot US execution (Kalshi perps + Coinbase US perps): shadow book, live switch, kill switch ──
import copy_live
import radio_proxy


class CopyLiveSettings(BaseModel):
    capital_cap: Optional[float] = None
    max_leverage: Optional[float] = None
    daily_loss_pct: Optional[float] = None
    venues: Optional[list[str]] = None


class CopyArm(BaseModel):
    confirm: str = ""
    override: str = ""


def _sim_stats():
    s = copy_sim.summary()
    return s["stats"]


@app.get("/api/copy/live/summary")
def api_copy_live_summary():
    return copy_live.summary(_sim_stats())


@app.post("/api/copy/live/test")
def api_copy_live_test():
    return copy_live.test_connections()


@app.post("/api/copy/live/settings")
def api_copy_live_settings(body: CopyLiveSettings):
    try:
        return copy_live.update_settings(**body.model_dump() if hasattr(body, "model_dump") else body.dict())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/copy/live/arm")
def api_copy_live_arm(body: CopyArm):
    try:
        return copy_live.arm(_sim_stats(), body.confirm, body.override)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/copy/live/disarm")
def api_copy_live_disarm():
    return copy_live.disarm()


@app.post("/api/copy/live/kill")
def api_copy_live_kill():
    return copy_live.kill()


@app.post("/api/copy/live/reset-shadow")
def api_copy_live_reset_shadow():
    copy_live.reset_shadow()
    return {"status": "reset"}


# ── Intel · Research Toolkit (OpenOSINT, OnionSearch, Crucix, link library) ──
import osint_tools


class ReconQ(BaseModel):
    kind: str
    target: str


class OnionQ(BaseModel):
    query: str
    limit: int = 2


@app.get("/api/intel/osint/inventory")
def osint_inventory():
    return osint_tools.inventory()


@app.post("/api/intel/osint/recon")
def osint_recon(q: ReconQ):
    try:
        return osint_tools.recon(q.kind, q.target)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/intel/osint/onion")
def osint_onion(q: OnionQ):
    try:
        return osint_tools.onion(q.query, q.limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/intel/osint/library")
def osint_library():
    return osint_tools.library()


@app.post("/api/intel/osint/crucix/{action}")
def osint_crucix(action: str):
    try:
        if action == "start":
            return osint_tools.crucix_start()
        if action == "stop":
            return osint_tools.crucix_stop()
        return osint_tools.crucix_status()
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── FBF Radio / music mini-player feeds (proxied so the browser has no CORS issues) ──
@app.get("/api/radio/playlists")
def api_radio_playlists():
    return radio_proxy.playlists()


@app.get("/api/radio/tracks")
def api_radio_tracks(playlist: str = "radio"):
    try:
        return radio_proxy.tracks(playlist)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ═══════════════════════════════════════════
#  HUB: link tiles, tile status, AI Coach, peptide research
# ═══════════════════════════════════════════
# ── God's Eye View (local 3D globe, C:\Tools\gods-eye-view) ──────────────────
GEV_URL = "http://127.0.0.1:4173"


def _gev_up(timeout=1.5):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4173), timeout=timeout):
            return True
    except OSError:
        return False


@app.post("/api/godseye/start")
def godseye_start():
    """Start the God's Eye View dev server if it isn't running, then report status."""
    import subprocess
    if not _gev_up():
        launcher = r"C:\Tools\gods-eye-view"
        if not os.path.isdir(launcher):
            raise HTTPException(404, "God's Eye View is not installed at C:\\Tools\\gods-eye-view")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen("npm run dev", cwd=launcher, shell=True, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if _gev_up():
                break
    return {"running": _gev_up(), "url": GEV_URL}


# ── Voice: speak coach answers in the FBF brand voice (local Chatterbox TTS, C:\AI\Chatterbox-TTS-Server) ──
from fastapi.responses import Response

TTS_URL = "http://127.0.0.1:8004"
TTS_DIR = r"C:\AI\Chatterbox-TTS-Server"
TTS_VOICE = os.environ.get("JARVIS_TTS_VOICE", "reference.wav")


def _tts_up():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8004), timeout=1):
            return True
    except OSError:
        return False


class TTSBody(BaseModel):
    text: str


import stt


@app.post("/api/stt")
async def speech_to_text(request: Request):
    """Athena's ears: browser posts the recorded utterance, Whisper on the GPU returns the words."""
    audio = await request.body()
    if len(audio) < 2000:
        return {"text": "", "secs": 0}
    ctype = request.headers.get("content-type", "")
    suffix = ".ogg" if "ogg" in ctype else ".mp4" if "mp4" in ctype else ".wav" if "wav" in ctype else ".webm"
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, stt.transcribe, audio, suffix)


@app.post("/api/say")
async def athena_say(request: Request):
    """Athena's voice: Microsoft Sonia neural (British female) via edge-tts, returned as MP3."""
    import edge_tts, hashlib
    body = await request.json()
    text = (body.get("text") or "").strip()[:1500]
    if not text:
        raise HTTPException(400, "no text")
    voice = os.environ.get("ATHENA_VOICE", "en-GB-SoniaNeural")
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tts_cache")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, hashlib.sha1((voice + text).encode()).hexdigest() + ".mp3")
    if not os.path.exists(path):
        await edge_tts.Communicate(text, voice, rate="+4%").save(path)
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="audio/mpeg")


@app.post("/api/stt/warm")
def stt_warm():
    import threading
    stt.warm()
    threading.Thread(target=hub.coach_warm, daemon=True).start()
    return {"ok": True}


@app.get("/api/tts/status")
def tts_status():
    return {"running": _tts_up(), "voice": TTS_VOICE}


@app.post("/api/tts/start")
def tts_start():
    """Start the local Chatterbox server (first start loads the model onto the GPU, ~30-60 s)."""
    import subprocess
    if not _tts_up():
        py = os.path.join(TTS_DIR, "venv", "Scripts", "python.exe")
        if not os.path.exists(py):
            raise HTTPException(404, "Chatterbox not installed")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([py, "server.py"], cwd=TTS_DIR, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"starting": True, "running": _tts_up()}


@app.post("/api/tts")
def tts(body: TTSBody):
    if not _tts_up():
        raise HTTPException(503, "Brand voice server not running")
    import urllib.request as _u
    req = _u.Request(TTS_URL + "/v1/audio/speech", method="POST", headers={"Content-Type": "application/json"},
                     data=json.dumps({"model": "chatterbox", "input": body.text[:1200], "voice": TTS_VOICE,
                                      "response_format": "mp3"}).encode())
    try:
        audio = _u.urlopen(req, timeout=120).read()
    except Exception as e:
        raise HTTPException(502, f"Voice generation failed: {e}")
    return Response(content=audio, media_type="audio/mpeg")


# ── Intel: supplier due diligence, contact verifier (anti-impersonation), batch check, peptide trends ──
import intel


class SupplierQ(BaseModel):
    domain: str = ""
    phone: str = ""
    name: str = ""


class ContactQ(BaseModel):
    channel: str
    value: str
    claimed_person: str = ""


class ContactAdd(BaseModel):
    kind: str            # verified | scam
    person: str = ""
    company: str = ""
    channel: str
    value: str
    note: str = ""


class BatchAdd(BaseModel):
    vendor: str
    compound: str = ""
    batch: str
    lab: str = ""
    report_id: str = ""
    purity: str = ""
    note: str = ""


class BatchQ(BaseModel):
    vendor: str
    batch: str
    compound: str = ""
    lab: str = ""
    report_id: str = ""


@app.post("/api/intel/supplier")
def intel_supplier(q: SupplierQ):
    if not (q.domain or q.phone):
        raise HTTPException(400, "Enter a website and/or phone number")
    return intel.supplier_check(q.domain, q.phone, q.name, secret=hub._secret)


@app.post("/api/intel/contact-check")
def intel_contact_check(q: ContactQ):
    return intel.contact_check(q.channel, q.value, q.claimed_person, secret=hub._secret)


# ── FBF app → Athena: live client adherence & progress (read-only coach session) ──
import fbf_clients


class FbfConnect(BaseModel):
    email: str
    password: str


@app.get("/api/fbf/status")
def fbf_status():
    return fbf_clients.status()


@app.post("/api/fbf/connect")
def fbf_connect(req: FbfConnect):
    try:
        return fbf_clients.connect(req.email, req.password)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/fbf/disconnect")
def fbf_disconnect():
    fbf_clients.disconnect()
    return {"ok": True}


@app.post("/api/fbf/refresh")
def fbf_refresh():
    return fbf_clients.refresh()


@app.get("/api/fbf/clients")
def fbf_clients_list():
    return {"status": fbf_clients.status(), "clients": fbf_clients.clients(), "alerts": fbf_clients.alerts()}


@app.get("/api/fbf/clients/{cid}")
def fbf_client_detail(cid: int):
    c = fbf_clients.client(cid)
    if not c:
        raise HTTPException(404, "Client not found (refresh, or they aren't assigned to you)")
    return c


@app.get("/api/fbf/clients/{cid}/adherence")
def fbf_client_adherence(cid: int):
    return fbf_client_detail(cid)["adherence"]


@app.get("/api/fbf/clients/{cid}/progress")
def fbf_client_progress(cid: int):
    return fbf_client_detail(cid)["progress"]


@app.get("/api/fbf/alerts")
def fbf_alerts():
    return {"alerts": fbf_clients.alerts(), "status": fbf_clients.status()}


@app.get("/api/intel/contacts")
def intel_contacts():
    return intel.contacts_list()


@app.post("/api/intel/contacts")
def intel_contacts_add(q: ContactAdd):
    try:
        return intel.contacts_add(q.kind, q.person, q.company, q.channel.lower(), q.value, q.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/intel/contacts/{kind}/{idx}")
def intel_contacts_del(kind: str, idx: int):
    return intel.contacts_remove(kind, idx)


@app.post("/api/intel/batches")
def intel_batch_add(q: BatchAdd):
    return intel.batch_add(q.vendor, q.compound, q.batch, q.lab, q.report_id, q.purity, q.note)


@app.post("/api/intel/batch-check")
def intel_batch_check(q: BatchQ):
    return intel.batch_check(q.vendor, q.batch, q.compound, q.lab, q.report_id)


@app.get("/api/intel/trends")
def intel_trends(terms: str = "", refresh: bool = False):
    t = [x for x in terms.split(",") if x.strip()] or None
    return intel.peptide_trends(t, refresh)


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

@app.post("/api/coach/stream")
def api_coach_stream(req: CoachReq):
    """NDJSON stream so Athena can start speaking while the answer is still being written."""
    import json as _json
    from fastapi.responses import StreamingResponse
    if not req.question.strip():
        raise HTTPException(400, "Ask a question")
    gen = (_json.dumps(ev) + "\n" for ev in hub.coach_stream(req.question, req.history))
    return StreamingResponse(gen, media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
