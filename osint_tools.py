"""
Research Toolkit for the Intel tab — the open-source tools from Bryan's intel-tools folder
(C:\\Users\\Antonelli\\jarvis-trading\\intel-tools), wired in properly.

Wired (businesses, domains, infrastructure, news, the dark-web keyword search):
  * OpenOSINT  — passive recon on a domain or IP: DNS + email security, WHOIS, RDAP,
                 IP / ASN, exposure grade, news mentions (GDELT), search-engine dork links
  * OnionSearch — keyword search across .onion search engines (needs Tor running)
  * Crucix     — local world-intel dashboard (27 public feeds), started on demand, port 3117
  * Link library — awesome-osint-arsenal + dark-web-osint-tools, searchable

Left installed in that folder but NOT wired here: the person-lookup tools (holehe, user-scanner,
toutatis, Mr.Holmes, tookie-osint, yesitsme, DaProfiler, Emora). This dashboard doesn't look up
private individuals.

Every tool runs as a subprocess with an argument list (no shell), with validated input and a timeout.
"""
from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("OSINT_TOOLS_DIR") or (Path.home() / "jarvis-trading" / "intel-tools"))
DATA = Path(__file__).parent / "data"
WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0

OPENOSINT = ROOT / "OpenOSINT" / ".venv" / ("Scripts/openosint.exe" if WIN else "bin/openosint")
ONIONSEARCH = ROOT / "OnionSearch" / ".venv" / ("Scripts/onionsearch.exe" if WIN else "bin/onionsearch")
CRUCIX_DIR = ROOT / "Crucix"
CRUCIX_PORT = 3117

DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]{3,39}$", re.I)
KEYWORD_RE = re.compile(r"^[\w .,'&+\-/]{2,80}$", re.U)

RECON = {   # command -> (label, accepts)
    "dns":      ("DNS & email security", "domain"),
    "whois":    ("WHOIS", "domain"),
    "rdap":     ("RDAP registration", "domain"),
    "exposure": ("Exposure grade (A–F)", "domain"),
    "ip":       ("IP / ASN / location", "ip"),
    "gdelt":    ("News mentions (GDELT)", "keyword"),
    "dorks":    ("Search-engine dork links", "domain"),
}

PERSON_TOOLS = {"holehe", "user-scanner", "toutatis", "mr-holmes", "Mr.Holmes", "tookie-osint",
                "yesitsme", "DaProfiler", "Emora-Project"}


def _clean_domain(d: str) -> str:
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def _run(args: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(cwd) if cwd else None, env=env, creationflags=NOWIN)


# ───────────────────────────────────────── OpenOSINT recon
def recon(kind: str, target: str) -> dict:
    if kind not in RECON:
        raise ValueError("Unknown check")
    if not OPENOSINT.exists():
        raise ValueError("OpenOSINT isn't installed (expected " + str(OPENOSINT) + ")")
    accepts = RECON[kind][1]
    t = (target or "").strip()
    if accepts == "domain":
        t = _clean_domain(t)
        if not DOMAIN_RE.match(t):
            raise ValueError("Enter a domain like example.com")
    elif accepts == "ip":
        if not IP_RE.match(t):
            raise ValueError("Enter an IP address")
    else:
        if not KEYWORD_RE.match(t):
            raise ValueError("Use 2–80 letters, numbers and basic punctuation")
    p = _run([str(OPENOSINT), "--json", "--no-pdf", kind, t], timeout=150)
    out = p.stdout
    i = out.find("{")
    if i < 0:
        return {"kind": kind, "label": RECON[kind][0], "target": t, "results": [],
                "error": (p.stderr or out or "no output").strip()[-600:]}
    try:
        d = json.loads(out[i:])
    except json.JSONDecodeError:
        return {"kind": kind, "label": RECON[kind][0], "target": t, "results": out[i:].splitlines()[:200], "error": None}
    res = d.get("results") or []
    if isinstance(res, str):
        res = res.splitlines()
    return {"kind": kind, "label": RECON[kind][0], "target": t, "results": [str(x) for x in res][:400],
            "error": d.get("error"), "at": d.get("timestamp")}


# ───────────────────────────────────────── OnionSearch
def tor_port() -> int | None:
    for p in (9050, 9150):     # Tor service / Tor Browser
        if _port_open(p):
            return p
    return None


def onion(query: str, limit: int = 2) -> dict:
    q = (query or "").strip()
    if not KEYWORD_RE.match(q):
        raise ValueError("Use 2–80 letters, numbers and basic punctuation")
    if not ONIONSEARCH.exists():
        raise ValueError("OnionSearch isn't installed")
    port = tor_port()
    if not port:
        raise ValueError("Tor isn't running. Open Tor Browser (or start the Tor service), then search again.")
    with tempfile.TemporaryDirectory() as td:
        outf = Path(td) / "out.csv"
        p = _run([str(ONIONSEARCH), "--proxy", f"127.0.0.1:{port}", "--limit", str(max(1, min(int(limit), 5))),
                  "--output", str(outf), "--fields", "engine", "name", "link", q], timeout=300)
        rows = []
        if outf.exists():
            with open(outf, encoding="utf-8", errors="replace") as f:
                for r in csv.reader(f):
                    if len(r) >= 3 and r[2].startswith("http"):
                        rows.append({"engine": r[0], "name": r[1][:200], "link": r[2]})
    seen, uniq = set(), []
    for r in rows:
        if r["link"] not in seen:
            seen.add(r["link"])
            uniq.append(r)
    return {"query": q, "results": uniq[:300], "count": len(uniq),
            "note": None if uniq else ((p.stderr or "")[-300:] or "No results — engines may be down; try again later.")}


# ───────────────────────────────────────── Crucix
_crucix_proc: subprocess.Popen | None = None
_crucix_lock = threading.Lock()


def crucix_status() -> dict:
    return {"installed": (CRUCIX_DIR / "node_modules").exists(), "running": _port_open(CRUCIX_PORT),
            "url": f"http://127.0.0.1:{CRUCIX_PORT}"}


def crucix_start() -> dict:
    global _crucix_proc
    with _crucix_lock:
        st = crucix_status()
        if st["running"]:
            return st
        if not st["installed"]:
            raise ValueError("Crucix dependencies aren't installed (run npm install in " + str(CRUCIX_DIR) + ")")
        DATA.mkdir(exist_ok=True)
        log = open(DATA / "crucix.log", "ab")
        _crucix_proc = subprocess.Popen(["node", "server.mjs"], cwd=str(CRUCIX_DIR), stdout=log, stderr=log,
                                        creationflags=NOWIN)
        for _ in range(40):
            time.sleep(0.5)
            if _port_open(CRUCIX_PORT):
                break
        return crucix_status()


def crucix_stop() -> dict:
    global _crucix_proc
    with _crucix_lock:
        if _crucix_proc and _crucix_proc.poll() is None:
            _crucix_proc.terminate()
            try:
                _crucix_proc.wait(5)
            except Exception:
                _crucix_proc.kill()
        _crucix_proc = None
        return crucix_status()


# ───────────────────────────────────────── link library
_lib_cache: tuple[float, list] | None = None
LINK_RE = re.compile(r"\[([^\]]{2,120})\]\((https?://[^)\s]+)\)")


def library() -> list[dict]:
    global _lib_cache
    if _lib_cache and time.time() - _lib_cache[0] < 3600:
        return _lib_cache[1]
    items, seen = [], set()
    for repo in ("awesome-osint-arsenal", "dark-web-osint-tools"):
        base = ROOT / repo
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            cat = md.stem if md.stem.lower() != "readme" else repo
            try:
                lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                h = re.match(r"^#{2,4}\s+(.*)", line)
                if h:
                    cat = re.sub(r"[^\w &/+\-.,()]", "", h.group(1)).strip() or cat
                    continue
                for m in LINK_RE.finditer(line):
                    name, url = m.group(1).strip(), m.group(2)
                    if url in seen or "img.shields.io" in url or url.endswith((".png", ".svg", ".gif")):
                        continue
                    seen.add(url)
                    desc = re.sub(LINK_RE, "", line).strip(" *-|–—:")
                    items.append({"name": name, "url": url, "category": cat[:60], "desc": desc[:200],
                                  "source": repo, "onion": ".onion" in url})
    _lib_cache = (time.time(), items)
    return items


def inventory() -> dict:
    folders = sorted(p.name for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []
    wired = {"OpenOSINT": OPENOSINT.exists(), "OnionSearch": ONIONSEARCH.exists(),
             "Crucix": (CRUCIX_DIR / "node_modules").exists(),
             "awesome-osint-arsenal": (ROOT / "awesome-osint-arsenal").exists(),
             "dark-web-osint-tools": (ROOT / "dark-web-osint-tools").exists()}
    return {"root": str(ROOT), "folders": folders, "wired": wired,
            "not_wired": [f for f in folders if f in PERSON_TOOLS],
            "recon_checks": {k: v[0] for k, v in RECON.items()}, "tor_port": tor_port(), "crucix": crucix_status()}
