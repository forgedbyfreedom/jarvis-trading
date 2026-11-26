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
    st = tor_start()                           # starts Tor in the background if needed
    port = st["port"]
    if not port or not st["ready"]:
        raise ValueError("Tor is still connecting — try the search again in a minute.")
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


# ───────────────────────────────────────── catalog: every tool in intel-tools, with links + status
def _vpy(folder: str) -> Path:
    return ROOT / folder / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")


def _vexe(folder: str, exe: str) -> Path:
    return ROOT / folder / ".venv" / (f"Scripts/{exe}.exe" if WIN else f"bin/{exe}")


CATALOG = [
    # name, repo, folder, what it does, how to run, setup line (run in that folder), ready-check
    ("OpenOSINT", "https://github.com/OpenOSINT/OpenOSINT", "OpenOSINT", "Passive recon framework (domain, DNS, WHOIS, IP, news)",
     r".venv\Scripts\openosint --help", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe -e .", lambda: _vexe("OpenOSINT", "openosint").exists()),
    ("OnionSearch", "https://github.com/megadose/OnionSearch", "OnionSearch", "Searches .onion search engines (needs Tor)",
     r".venv\Scripts\onionsearch --help", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe .", lambda: _vexe("OnionSearch", "onionsearch").exists()),
    ("Crucix", "https://github.com/calesthio/Crucix", "Crucix", "Local world-intel dashboard, 27 public feeds",
     "npm start  (or the Start button below)", "npm install", lambda: (ROOT / "Crucix" / "node_modules").exists()),
    ("Awesome OSINT Arsenal", "https://github.com/rawfilejson/awesome-osint-arsenal", "awesome-osint-arsenal", "753+ tool link list (searchable below)",
     "—", "—", lambda: (ROOT / "awesome-osint-arsenal").exists()),
    ("Dark Web OSINT Tools", "https://github.com/apurvsinghgautam/dark-web-osint-tools", "dark-web-osint-tools", "Dark-web tool link list (searchable below)",
     "—", "—", lambda: (ROOT / "dark-web-osint-tools").exists()),
    ("Holehe", "https://github.com/megadose/holehe", "holehe", "Checks which sites an email is registered on",
     r".venv\Scripts\holehe --help", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe .", lambda: _vexe("holehe", "holehe").exists()),
    ("User Scanner", "https://github.com/kaifcodec/user-scanner", "user-scanner", "Username / email presence across sites",
     r".venv\Scripts\user-scanner --help", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe .", lambda: _vexe("user-scanner", "user-scanner").exists()),
    ("Toutatis", "https://github.com/megadose/toutatis", "toutatis", "Instagram account info (needs an IG session id)",
     r".venv\Scripts\toutatis -h", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe .", lambda: _vexe("toutatis", "toutatis").exists()),
    ("Mr.Holmes", "https://github.com/Lucksi/Mr.Holmes", "Mr.Holmes", "Interactive info-gathering framework",
     "Install.cmd once, then: python MrHolmes.py", "Install.cmd", lambda: (ROOT / "Mr.Holmes" / "MrHolmes.py").exists() and (ROOT / "Mr.Holmes" / "Configuration").exists()),
    ("Tookie OSINT", "https://github.com/Alfredredbird/tookie-osint", "tookie-osint", "Username search across sites",
     r".venv\Scripts\python brib.py", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe -r requirements.txt", lambda: _vpy("tookie-osint").exists()),
    ("YesItsMe", "https://github.com/0x0be/yesitsme", "yesitsme", "Finds Instagram profiles by name + email/phone hints",
     r".venv\Scripts\python yesitsme.py -h", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe -r requirements.txt", lambda: _vpy("yesitsme").exists()),
    ("DaProfiler", "https://github.com/daprofiler/DaProfiler", "DaProfiler", "Builds a profile from a name (FR-focused; needs Firefox)",
     r".venv\Scripts\python profiler.py -h", "uv venv .venv; uv pip install --python .venv\\Scripts\\python.exe -r requirements.txt", lambda: _vpy("DaProfiler").exists()),
    ("Emora", "https://github.com/IdefaSoft/Emora-Project", "Emora-Project", "Windows GUI username search (121 sites)",
     "Download the v1.3.0 release .exe, or build Emora.sln in Visual Studio", "Releases page", lambda: any((ROOT / "Emora-Project").rglob("Emora.exe"))),
]


def catalog() -> list[dict]:
    out = []
    for name, repo, folder, what, run, setup, ready in CATALOG:
        path = ROOT / folder
        try:
            ok = bool(ready())
        except Exception:
            ok = False
        out.append({"name": name, "repo": repo, "folder": str(path), "present": path.exists(), "ready": ok,
                    "what": what, "run": run, "setup": setup,
                    "releases": repo + "/releases" if name == "Emora" else None})
    return out


def open_folder(name: str) -> str:
    for n, _, folder, *_ in CATALOG:
        if n == name:
            p = ROOT / folder
            if not p.exists():
                raise ValueError("Folder not found")
            if WIN:
                os.startfile(str(p))          # opens Explorer on the PC
            return str(p)
    raise ValueError("Unknown tool")


# ───────────────────────────────────────── launch a tool on the PC (opens it ready to use)
JT_VENV_PY = ROOT.parent / "venv" / "Scripts" / "python.exe"     # where Mr.Holmes' Install.cmd put its packages

LAUNCH = {  # name -> (folder, PowerShell command run in a new window; fixed strings, no user input)
    "OpenOSINT":   ("OpenOSINT",   r"& .\.venv\Scripts\Activate.ps1; openosint --help"),
    "OnionSearch": ("OnionSearch", r"& .\.venv\Scripts\Activate.ps1; onionsearch --help; Write-Host 'Needs Tor Browser open. Example:  onionsearch --proxy 127.0.0.1:9150 --limit 2 search words'"),
    "Holehe":      ("holehe",      r"& .\.venv\Scripts\Activate.ps1; holehe --help; Write-Host 'Example: holehe --only-used name@example.com'"),
    "User Scanner": ("user-scanner", r"& .\.venv\Scripts\Activate.ps1; user-scanner --help"),
    "Toutatis":    ("toutatis",    r"& .\.venv\Scripts\Activate.ps1; toutatis -h"),
    "Tookie OSINT": ("tookie-osint", r"& .\.venv\Scripts\Activate.ps1; python brib.py -h"),
    "YesItsMe":    ("yesitsme",    r"& .\.venv\Scripts\Activate.ps1; python yesitsme.py -h"),
    "DaProfiler":  ("DaProfiler",  r"& .\.venv\Scripts\Activate.ps1; python profiler.py -h"),
    "Mr.Holmes":   ("Mr.Holmes",   r"& '..\..\venv\Scripts\Activate.ps1'; python MrHolmes.py"),
}


def launch(name: str) -> dict:
    if name == "Crucix":
        st = crucix_start()
        return {"url": st["url"], "running": st["running"]}
    if name == "Emora":
        exe = next((ROOT / "Emora-Project").rglob("Emora.exe"), None)
        if not exe:
            raise ValueError("Emora isn't downloaded yet — use the Releases link and unzip it into the Emora-Project folder")
        if WIN:
            os.startfile(str(exe))
        return {"opened": str(exe)}
    if name in ("Awesome OSINT Arsenal", "Dark Web OSINT Tools"):
        return {"scroll": "library"}
    if name not in LAUNCH:
        raise ValueError("Unknown tool")
    folder, cmd = LAUNCH[name]
    cwd = ROOT / folder
    if not cwd.exists():
        raise ValueError("Folder not found: " + str(cwd))
    if WIN:
        subprocess.Popen(["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
                          f"$Host.UI.RawUI.WindowTitle='{name}'; {cmd}"],
                         cwd=str(cwd), creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {"opened": str(cwd)}


# ───────────────────────────────────────── Tor: auto-start a background tor.exe (no Tor Browser window)
_tor_proc: subprocess.Popen | None = None
_tor_lock = threading.Lock()
TOR_LOG = DATA / "tor.log"


def _find_tor() -> Path | None:
    home = Path.home()
    cands = [
        os.environ.get("TOR_EXE", ""),
        home / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        home / "OneDrive" / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path(r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe"),
        Path(r"C:\Tools\tor\tor\tor.exe"), Path(r"C:\Tools\tor\tor.exe"),      # Tor Expert Bundle
        ROOT / "tor" / "tor" / "tor.exe", ROOT / "tor" / "tor.exe",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def tor_status() -> dict:
    port = tor_port()
    ready = bool(port)
    if port == 9050 and _tor_proc and _tor_proc.poll() is None:
        try:
            ready = "Bootstrapped 100%" in TOR_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ready = False
    exe = _find_tor()
    return {"port": port, "ready": ready, "installed": bool(exe), "exe": str(exe) if exe else None}


def tor_start(wait: float = 90) -> dict:
    """Start tor.exe in the background on 127.0.0.1:9050 if no Tor is already running."""
    global _tor_proc
    with _tor_lock:
        if tor_port() and not (_tor_proc and _tor_proc.poll() is None):
            return tor_status()                    # Tor Browser or a Tor service already running
        if not (_tor_proc and _tor_proc.poll() is None):
            exe = _find_tor()
            if not exe:
                raise ValueError("Tor isn't installed. Install Tor Browser (torproject.org) once — after that "
                                 "the Command Center starts Tor by itself, no browser window needed.")
            DATA.mkdir(exist_ok=True)
            ddir = DATA / "tor-data"
            ddir.mkdir(exist_ok=True)
            TOR_LOG.write_text("", encoding="utf-8")
            log = open(TOR_LOG, "ab")
            _tor_proc = subprocess.Popen([str(exe), "--SocksPort", "127.0.0.1:9050", "--DataDirectory", str(ddir),
                                          "--Log", "notice stdout", "--ClientOnly", "1"],
                                         cwd=str(exe.parent), stdout=log, stderr=log, creationflags=NOWIN)
    end = time.time() + wait
    while time.time() < end:
        st = tor_status()
        if st["ready"]:
            return st
        if _tor_proc and _tor_proc.poll() is not None:
            raise ValueError("Tor exited — see data\\tor.log")
        time.sleep(1)
    return tor_status()


def tor_stop() -> dict:
    global _tor_proc
    with _tor_lock:
        if _tor_proc and _tor_proc.poll() is None:
            _tor_proc.terminate()
        _tor_proc = None
    return tor_status()
