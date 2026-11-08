# Vendored from github.com/forgedbyfreedom/copybot (read-only data + sizing logic; no order code).
"""Config, state, logging, HTTP and notification plumbing shared by all modules."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
try:
    import yaml  # only needed by load_config, which Jarvis does not use
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("copybot")

USER_AGENT = "FBF-CopyBot/1.0 (personal trading research; contact via GitHub)"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ config
def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("COPYBOT_CONFIG") or ROOT / "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def is_live(cfg: dict[str, Any], force_dry: bool = False) -> bool:
    """Live only when config says live AND the LIVE_TRADING env flag is set AND no kill switch."""
    if force_dry or cfg.get("kill_switch"):
        return False
    if os.environ.get("COPYBOT_KILL", "").lower() in ("1", "true", "yes"):
        return False
    return cfg.get("mode") == "live" and os.environ.get("LIVE_TRADING", "").lower() == "true"


def kill_switch_on(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("kill_switch")) or os.environ.get("COPYBOT_KILL", "").lower() in ("1", "true", "yes")


# ------------------------------------------------------------------ state
class State:
    """JSON state file committed back to the repo between scheduled runs."""

    def __init__(self, path: str | Path | None = None, job: str = "state"):
        base = Path(os.environ.get("COPYBOT_STATE_DIR") or ROOT / "state")
        self.path = Path(path) if path else base / f"{job}.json"
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                log.error("state file corrupt; starting fresh (old copy kept as .bak)")
                self.path.rename(self.path.with_suffix(".bak"))
                self.data = {}

    def section(self, name: str) -> dict[str, Any]:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(self.path)


# ------------------------------------------------------------------ http
_session: requests.Session | None = None


def http() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def get_with_retry(url: str, *, tries: int = 3, timeout: float = 30, **kw) -> requests.Response:
    last: Exception | None = None
    for i in range(tries):
        try:
            r = http().get(url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


def post_json_with_retry(url: str, payload: dict, *, tries: int = 3, timeout: float = 30) -> Any:
    last: Exception | None = None
    for i in range(tries):
        try:
            r = http().post(url, json=payload, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"POST failed after {tries} tries: {url}: {last}")


# ------------------------------------------------------------------ reporting
@dataclass
class Report:
    title: str
    live: bool
    lines: list[str] = field(default_factory=list)
    trades: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def info(self, msg: str) -> None:
        log.info(msg)
        self.lines.append(msg)

    def trade(self, msg: str) -> None:
        log.info("TRADE %s", msg)
        self.trades.append(msg)

    def error(self, msg: str) -> None:
        log.error(msg)
        self.errors.append(msg)

    def markdown(self) -> str:
        mode = "🔴 LIVE" if self.live else "🟡 DRY RUN"
        out = [f"## {self.title} — {mode}", f"_{utcnow():%Y-%m-%d %H:%M UTC}_", ""]
        if self.trades:
            out += ["### Orders", *[f"- {t}" for t in self.trades], ""]
        else:
            out += ["No orders this run.", ""]
        if self.errors:
            out += ["### Errors", *[f"- ⚠️ {e}" for e in self.errors], ""]
        if self.lines:
            out += ["<details><summary>Log</summary>", "", *[f"- {l}" for l in self.lines], "</details>"]
        return "\n".join(out)

    def publish(self, cfg: dict[str, Any]) -> None:
        md = self.markdown()
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(md + "\n\n")
        hook = os.environ.get("NOTIFY_WEBHOOK_URL")
        ncfg = cfg.get("notify", {})
        want = (self.trades and ncfg.get("on_trade", True)) or (self.errors and ncfg.get("on_error", True))
        if hook and want:
            mode = "LIVE" if self.live else "DRY RUN"
            body = f"**{self.title} ({mode})**\n" + "\n".join(
                [f"• {t}" for t in self.trades] + [f"⚠️ {e}" for e in self.errors]
            )
            try:
                # Discord uses "content", Slack uses "text"; send both.
                http().post(hook, json={"content": body[:1900], "text": body[:3000]}, timeout=15)
            except Exception as e:  # noqa: BLE001
                log.error("webhook failed: %s", e)
