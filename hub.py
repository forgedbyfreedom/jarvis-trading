"""
Command Center hub: link tiles, live tile status, the private FBF AI Coach
proxy, and the daily peptide-research feed (PubMed).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

import config as C

log = logging.getLogger("jarvis.hub")
BASE = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(C.DATA_DIR, "links.json")
PEPTIDE_FILE = os.path.join(C.DATA_DIR, "peptides.json")


SECRET_FILES = [
    os.path.join(BASE, ".env.local"),                                   # preferred, git-ignored
    os.path.join(os.path.expanduser("~"), "fbf-ops", "credentials.env"),  # FBF ops credentials on this PC
]


def _env_file() -> dict:
    """Secrets are read from local files at runtime, never stored in the repo."""
    out = {}
    for path in reversed(SECRET_FILES):
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _secret(name: str, default: str = "") -> str:
    return os.environ.get(name) or _env_file().get(name, default)


# ───────────────────────────── link tiles
DEFAULT_LINKS = {
    "coaching":  {"title": "Coaching Dashboard", "url": "https://app.forgedbyfreedom.org"},
    "website":   {"title": "FBF Website", "url": "https://forgedbyfreedom.net"},
    "ezform":    {"title": "EZForm · Peptide Catalog", "url": "https://ezformz.net/f/fbf-peptide-catalog-ew6n4u"},
    "ezform2":   {"title": "EZForm · Special Stuff", "url": "https://ezformz.net/f/fbf-peptide-catalog-juice"},
    "ezedit":    {"title": "EZForm Builder", "url": "https://ezformz.net/edit/8db490fe-2e5d-4e90-9c42-7fb6cfe41c8e"},
    "sports":    {"title": "FBF Sports Board", "url": "https://data.forgedbyfreedom.org/board.html"},
    "venmo":     {"title": "Venmo", "url": "https://account.venmo.com/"},
    "cashapp":   {"title": "Cash App", "url": "https://cash.app/account"},
    "store":     {"title": "Merch Store (Etsy)", "url": "https://www.etsy.com/your/shops/me/dashboard"},
    "bloodwork": {"title": "Bloodwork Analyzer", "url": "https://forgedbyfreedom.net/ai-coach/"},
    "wpadmin":   {"title": "WordPress Admin", "url": "https://forgedbyfreedom.net/wp-admin/"},
    "telegram":  {"title": "Telegram", "url": "https://t.me/BalanceNomad"},
    "whatsapp":  {"title": "WhatsApp", "url": "https://wa.me/17757418213"},
    "calendar":  {"title": "Calendar", "url": "https://calendly.com/forgedbyfreedom"},
    "gmail":     {"title": "Gmail", "url": "https://mail.google.com/"},
    "proton":    {"title": "Proton Mail", "url": "https://mail.proton.me/"},
    "github":    {"title": "GitHub", "url": "https://github.com/forgedbyfreedom"},
    "editsuite": {"title": "FBF Edit Suite", "url": "http://127.0.0.1:8790"},
}


def links() -> dict:
    try:
        saved = json.load(open(LINKS_FILE, encoding="utf-8"))
    except Exception:
        saved = {}
    out = {k: dict(v) for k, v in DEFAULT_LINKS.items()}
    for k, v in saved.items():
        out.setdefault(k, {}).update(v)
    return out


def set_link(key: str, url: str, title: str | None = None) -> dict:
    try:
        saved = json.load(open(LINKS_FILE, encoding="utf-8"))
    except Exception:
        saved = {}
    saved.setdefault(key, {})["url"] = url
    if title:
        saved[key]["title"] = title
    os.makedirs(C.DATA_DIR, exist_ok=True)
    json.dump(saved, open(LINKS_FILE, "w", encoding="utf-8"), indent=2)
    return links()


# ───────────────────────────── AI service
AI_URL = lambda: _secret("FBF_AI_URL", "https://ai.serverborn.com").rstrip("/")


def ai_health() -> dict:
    try:
        r = requests.get(AI_URL() + "/health", timeout=8)
        d = r.json()
        return {"ok": r.ok, **d}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def coach_ask(question: str, history: list | None = None) -> dict:
    key = _secret("FBF_AI_API_KEY")
    if not key:
        return {"error": "FBF_AI_API_KEY is not set in C:\\jarvis-trading\\.env.local"}
    q = question.strip()
    if history:  # the service is single-turn; carry the last exchanges as context
        ctx = "\n".join(f"{'Me' if m.get('role') == 'user' else 'Coach'}: {m.get('text', '')[:600]}" for m in history[-6:])
        q = f"Earlier in this conversation:\n{ctx}\n\nMy new question: {q}"
    t0 = time.time()
    r = requests.post(AI_URL() + "/ask", json={"question": q}, headers={"X-API-Key": key}, timeout=120)
    r.raise_for_status()
    d = r.json()
    d["seconds"] = round(time.time() - t0, 1)
    return d


# ───────────────────────────── peptide research (PubMed, refreshed daily)
PEPTIDES = [
    ("BPC-157", '"BPC-157" OR "BPC 157" OR "body protection compound"'),
    ("TB-500 / Thymosin β4", '"thymosin beta-4" OR "thymosin beta 4" OR "TB-500"'),
    ("Semaglutide", "semaglutide"), ("Tirzepatide", "tirzepatide"), ("Retatrutide", "retatrutide"),
    ("Cagrilintide", "cagrilintide OR CagriSema"), ("Orforglipron", "orforglipron"),
    ("Mazdutide / Survodutide", "mazdutide OR survodutide"), ("Amycretin / Eloralintide", "amycretin OR eloralintide"),
    ("Tesamorelin", "tesamorelin"), ("CJC-1295 / Ipamorelin", '"CJC-1295" OR ipamorelin OR sermorelin'),
    ("GHK-Cu", '"GHK-Cu" OR "copper peptide GHK" OR "GHK copper"'), ("MOTS-c", '"MOTS-c"'),
    ("Epitalon", "epitalon OR epithalon"), ("Selank / Semax", "selank OR semax"),
    ("Kisspeptin", "kisspeptin"), ("PT-141 / Bremelanotide", "bremelanotide OR PT-141"),
    ("Thymosin α1", '"thymosin alpha 1" OR thymalfasin'), ("AOD-9604", '"AOD-9604" OR "AOD9604"'),
    ("SS-31 / Elamipretide", "elamipretide OR SS-31"), ("Tesofensine", "tesofensine"),
]
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_pep_lock = threading.Lock()


def _eutil(path: str, **params):
    params.update(tool="fbf-command-center", retmode="json")
    for attempt in range(3):
        r = requests.get(f"{EUTILS}/{path}", params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1)); continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def refresh_peptides(days: int = 7) -> dict:
    with _pep_lock:
        found: dict[str, dict] = {}
        counts = {}
        for name, term in PEPTIDES:
            try:
                d = _eutil("esearch.fcgi", db="pubmed", term=f"({term})", reldate=days, datetype="edat", retmax=40, sort="pub_date")
                ids = d.get("esearchresult", {}).get("idlist", [])
                counts[name] = int(d.get("esearchresult", {}).get("count", len(ids)))
                for pmid in ids:
                    found.setdefault(pmid, {"pmid": pmid, "peptides": []})["peptides"].append(name)
            except Exception as e:
                log.warning("pubmed %s: %s", name, e)
                counts[name] = None
            time.sleep(0.4)  # NCBI: max 3 requests/second without an API key
        ids = list(found)
        for i in range(0, len(ids), 150):
            try:
                s = _eutil("esummary.fcgi", db="pubmed", id=",".join(ids[i:i + 150]))
                for pmid in ids[i:i + 150]:
                    it = s.get("result", {}).get(pmid, {})
                    found[pmid].update(
                        title=it.get("title", ""), journal=it.get("fulljournalname") or it.get("source", ""),
                        pubdate=it.get("pubdate", ""), epubdate=it.get("epubdate", ""),
                        entrez=it.get("sortpubdate", ""), authors=", ".join(a.get("name", "") for a in it.get("authors", [])[:3]),
                        types=it.get("pubtype", []), url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
            except Exception as e:
                log.warning("pubmed summary: %s", e)
            time.sleep(0.4)
        papers = sorted(found.values(), key=lambda p: p.get("entrez", ""), reverse=True)
        out = {"refreshed": datetime.now().isoformat(timespec="minutes"), "days": days,
               "counts": counts, "papers": papers}
        os.makedirs(C.DATA_DIR, exist_ok=True)
        json.dump(out, open(PEPTIDE_FILE, "w", encoding="utf-8"), indent=1)
        return out


def peptides(force: bool = False) -> dict:
    try:
        d = json.load(open(PEPTIDE_FILE, encoding="utf-8"))
        age_h = (datetime.now() - datetime.fromisoformat(d["refreshed"])).total_seconds() / 3600
        if not force and age_h < 24:
            return d
    except Exception:
        d = None
    if force or d is None:
        return refresh_peptides()
    threading.Thread(target=refresh_peptides, daemon=True).start()  # serve stale copy, refresh behind
    return d


def _daily_peptide_loop():
    while True:
        try:
            peptides()
        except Exception as e:
            log.warning("peptide refresh: %s", e)
        time.sleep(3600)


def start() -> None:
    threading.Thread(target=_daily_peptide_loop, daemon=True, name="peptides").start()
