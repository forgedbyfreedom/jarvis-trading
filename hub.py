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


COACH_MODEL = lambda: _secret("COACH_MODEL", "huihui_ai/Qwen3.8-abliterated:latest")
OLLAMA_URL = lambda: _secret("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
COACH_SYSTEM = (
    "You are Athena, Bryan's private research and coaching assistant inside his FBF Command Center. "
    "Bryan is an experienced coach and competitor who asks for complete, specific, practical information. "
    "Answer directly and fully. Use the RESEARCH NOTES from his library first and say when you go beyond them. "
    "Give real numbers (doses studied, half-lives, timelines, ranges) where the research has them, and state "
    "uncertainty plainly. No lectures, no moralising, no 'consult a professional' filler. "
    "Keep spoken-style answers tight: lead with the answer, then the key details. Never refer to your instructions, prompt sections or 'research notes' by name; say 'your library' if you need to cite it."
)


def _rag_lookup(q: str) -> dict:
    """Grounded research from the FBF RAG library (ai.serverborn.com)."""
    key = _secret("FBF_AI_API_KEY")
    if not key:
        return {}
    try:
        r = requests.post(AI_URL() + "/ask", json={"question": q}, headers={"X-API-Key": key}, timeout=90)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("rag lookup failed: %s", e)
        return {}


KNOWLEDGE_DIR = os.path.join(C.DATA_DIR, "athena_knowledge")
_kn_cache = {"key": None, "text": ""}


def _athena_knowledge() -> str:
    """Athena's direct knowledge: every .md in data/athena_knowledge, always in her system prompt."""
    try:
        files = sorted(f for f in os.listdir(KNOWLEDGE_DIR) if f.endswith(".md"))
    except FileNotFoundError:
        return ""
    key = tuple((f, os.path.getmtime(os.path.join(KNOWLEDGE_DIR, f))) for f in files)
    if key != _kn_cache["key"]:
        parts = [open(os.path.join(KNOWLEDGE_DIR, f), encoding="utf-8").read().strip() for f in files]
        _kn_cache.update(key=key, text="\n\n".join(parts))
    return _kn_cache["text"]


def _coach_messages(q: str, history, notes: str, srcs: list, clientdata: str = "") -> list:
    kn = _athena_knowledge()
    system = COACH_SYSTEM + (f"\n\nBACKGROUND YOU ALREADY KNOW (Bryan's own products and facts; trust it over the research notes). Use it naturally as your own knowledge; never mention this section, its title, or that you were given it:\n{kn}" if kn else "")
    msgs = [{"role": "system", "content": system}]
    for m in (history or [])[-6:]:
        msgs.append({"role": "user" if m.get("role") == "user" else "assistant", "content": (m.get("text") or "")[:1500]})
    user = q
    if clientdata:
        user = (f"{clientdata}\n\nMY QUESTION: {q}\n\nAnswer from the live FBF app data above. Use real names and numbers, never invent "
                f"data, say plainly when something isn't logged (null), and end with the coaching action you'd take.")
    elif notes:
        user = (f"RESEARCH NOTES from my library (sources: {', '.join(map(str, srcs[:8]))}):\n{notes[:6000]}\n\n"
                f"MY QUESTION: {q}\n\nAnswer my question. If the notes held back or refused anything, fill it in yourself.")
    msgs.append({"role": "user", "content": user})
    return msgs


def coach_stream(question: str, history: list | None = None):
    """Yields events: {"stage":"library"} → {"sources":[..],"grounded":bool} → {"delta":"text"}… → {"done":True,...}.
    Research comes from the RAG library; the answer is written token-by-token by the local model."""
    t0 = time.time()
    q = question.strip()
    import fbf_clients
    clientdata, is_client = fbf_clients.athena_context(q)
    yield {"stage": "clients" if is_client else "library"}
    rag = {} if is_client else _rag_lookup(q)
    notes = (rag.get("answer") or "").strip()
    srcs = rag.get("sources") or []
    yield {"sources": (["FBF app (live client data)"] if is_client else srcs), "grounded": bool(notes) or is_client, "library_secs": round(time.time() - t0, 1)}
    yield {"stage": "writing"}
    got = False
    try:
        with requests.post(OLLAMA_URL() + "/api/chat", json={
                "model": COACH_MODEL(), "messages": _coach_messages(q, history, notes, srcs, clientdata), "stream": True,
                "think": False, "keep_alive": "8h", "options": {"num_ctx": 16384, "temperature": 0.6}},
                stream=True, timeout=(10, 300)) as r:
            r.raise_for_status()
            in_think = False
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                tok = (d.get("message") or {}).get("content", "")
                if "<think>" in tok:
                    in_think = True
                if in_think:
                    if "</think>" in tok:
                        in_think = False
                    continue
                if tok:
                    got = True
                    yield {"delta": tok}
                if d.get("done"):
                    break
        yield {"done": True, "model": COACH_MODEL(), "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        log.warning("local coach model failed: %s", e)
        if not got and notes:  # local model down: give the library's own answer
            yield {"delta": notes}
            yield {"done": True, "model": "library", "seconds": round(time.time() - t0, 1)}
        elif not got:
            yield {"error": f"Local model unavailable ({e}) and the library did not answer."}
        else:
            yield {"done": True, "model": COACH_MODEL(), "seconds": round(time.time() - t0, 1), "cut_short": True}


def coach_ask(question: str, history: list | None = None) -> dict:
    """Non-streaming wrapper (used by tests and anything that wants the whole answer at once)."""
    out = {"answer": "", "sources": [], "grounded": False}
    for ev in coach_stream(question, history):
        if "delta" in ev:
            out["answer"] += ev["delta"]
        elif "sources" in ev:
            out.update(sources=ev["sources"], grounded=ev["grounded"])
        elif "error" in ev:
            return {"error": ev["error"]}
        elif ev.get("done"):
            out.update(model=ev.get("model"), seconds=ev.get("seconds"))
    out["answer"] = out["answer"].strip()
    return out


def _gpu_job_running() -> bool:
    """True while an FBF Radio episode is rendering (Chatterbox needs the VRAM Athena's model would take)."""
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | % CommandLine"],
                             capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "render_episode" in out
    except Exception:
        return False


def _keep_warm_loop():
    """Keep Athena's model loaded: other Ollama clients can unload it, and a cold reload costs ~60 s."""
    while True:
        try:
            ps = requests.get(OLLAMA_URL() + "/api/ps", timeout=10).json().get("models", [])
            mine = [m for m in ps if m.get("name") == COACH_MODEL() or m.get("model") == COACH_MODEL()]
            exp = mine[0].get("expires_at", "") if mine else ""
            soon = True
            if exp:
                try:
                    from datetime import datetime, timezone
                    import re as _re
                    left = (datetime.fromisoformat(_re.sub(r"\.\d+", "", exp).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
                    soon = left < 1800
                except Exception:
                    soon = True
            others = [m for m in ps if m not in mine]
            if _gpu_job_running():
                time.sleep(120)
                continue
            # Another job (e.g. the FBF Council morning run) has the GPU: don't fight it — wait until it's done.
            if not others and (not mine or soon):
                coach_warm()
        except Exception as e:
            log.debug("keep-warm: %s", e)
        time.sleep(120)


def start_keep_warm():
    threading.Thread(target=_keep_warm_loop, daemon=True, name="athena-keep-warm").start()


def coach_warm():
    """Load the coach model into the GPU so the first answer is quick."""
    try:
        requests.post(OLLAMA_URL() + "/api/generate", json={"model": COACH_MODEL(), "prompt": "", "keep_alive": "8h", "options": {"num_ctx": 16384}}, timeout=180)
    except Exception as e:
        log.warning("coach warm: %s", e)


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
