"""FBF app → Athena bridge.

Reads live client data from the FBF Coach Hub (WordPress, namespace fbf/v1) with Bryan's coach
session, turns it into adherence / progress snapshots, raises coaching alerts, and hands Athena a
compact text briefing when a question is about clients.

Only the App Bridge session token is stored (data/fbf_session.json) — never the password.
Snapshots are cached in data/fbf_clients.json and refreshed every POLL_MIN minutes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

import config as C

log = logging.getLogger("jarvis.fbf")

WP = os.environ.get("FBF_WP_URL", "https://forgedbyfreedom.net").rstrip("/")
API = WP + "/wp-json/fbf/v1"
SESSION_FILE = os.path.join(C.DATA_DIR, "fbf_session.json")
CACHE_FILE = os.path.join(C.DATA_DIR, "fbf_clients.json")
POLL_MIN = int(os.environ.get("FBF_POLL_MIN", "30"))
UA = {"User-Agent": "FBF-Command-Center/1.0 (Athena)"}

_lock = threading.RLock()
_state = {"clients": {}, "alerts": [], "updated_at": None, "error": None, "new_alerts": 0}


# ───────────────────────── session ─────────────────────────
def _session() -> dict:
    try:
        return json.load(open(SESSION_FILE, encoding="utf-8"))
    except Exception:
        return {}


def connected() -> bool:
    return bool(_session().get("token"))


def connect(email: str, password: str) -> dict:
    """One-time sign-in with Bryan's FBF coach login. Stores the session token only."""
    r = requests.post(API + "/auth/login", json={"email": email.strip(), "password": password}, headers=UA, timeout=30)
    try:
        d = r.json()
    except Exception:
        d = {}
    if not r.ok or not d.get("token"):
        raise ValueError(d.get("message") or f"Sign-in failed ({r.status_code})")
    sess = {"token": d["token"], "user": d.get("user", {}), "connected_at": _now()}
    os.makedirs(C.DATA_DIR, exist_ok=True)
    json.dump(sess, open(SESSION_FILE, "w", encoding="utf-8"), indent=2)
    me = _get("/coach/me")
    if not me.get("is_coach"):
        disconnect()
        raise ValueError("That login isn't a coach account in the FBF app.")
    sess["coach"] = me
    json.dump(sess, open(SESSION_FILE, "w", encoding="utf-8"), indent=2)
    threading.Thread(target=refresh, daemon=True).start()
    return {"ok": True, "user": sess["user"], "coach": me}


def disconnect():
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass


class SessionExpired(Exception):
    pass


def _get(path: str, **params):
    tok = _session().get("token")
    if not tok:
        raise SessionExpired("Not connected to the FBF app")
    r = requests.get(API + path, params=params, headers={**UA, "Authorization": f"Bearer {tok}"}, timeout=45)
    if r.status_code == 401:
        raise SessionExpired("FBF app session expired — reconnect in the FBF Clients tab")
    r.raise_for_status()
    return r.json()


# ───────────────────────── maths ─────────────────────────
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _pct(num, den):
    return None if not den else round(100.0 * num / den, 1)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def adherence(daily: list, checkins: list, window: int = 14) -> dict:
    """Adherence over the last `window` days from the app's daily log. Missing data stays null."""
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=window - 1)).isoformat()
    rows = sorted([r for r in daily if r.get("day", "") >= since], key=lambda r: r["day"])
    past = [r for r in rows if r["day"] < today]
    done = missed = 0
    streak = worst_streak = 0
    meals_p = meals_e = off_meals = 0
    water_days = water_hit = 0
    sup_p = sup_t = 0
    off_days = []
    for r in rows:
        d = r.get("data") or {}
        is_past = r["day"] < today
        if d.get("trained"):
            done += 1
            streak = 0
        elif d.get("training_day") and is_past:
            missed += 1
            streak += 1
            worst_streak = max(worst_streak, streak)
        if d.get("meals_planned"):
            meals_p += d.get("meals_planned") or 0
            meals_e += d.get("meals_eaten") or 0
        off_meals += d.get("off_plan_meals") or 0
        if d.get("water_target_oz") and (is_past or d.get("water_oz")):
            water_days += 1
            water_hit += (d.get("water_oz") or 0) >= 0.9 * d["water_target_oz"]
        if d.get("supplements_planned"):
            sup_p += d.get("supplements_planned") or 0
            sup_t += d.get("supplements_taken") or 0
        if is_past and r.get("shortfalls"):
            off_days.append({"day": r["day"], "shortfalls": r["shortfalls"]})
    last = checkins[0] if checkins else None
    ld = _d(last["created_at"]) if last else None
    if not last:
        ck = "none"
    elif ld and (datetime.now(timezone.utc) - ld).days > 8:
        ck = "overdue"
    else:
        ck = "reviewed" if last.get("feedback") else "pending_review"
    return {
        "window_days": window,
        "days_logged": len(rows),
        "logging_rate_pct": _pct(len(past), max(window - 1, 1)),
        "workouts_completed": done,
        "workouts_missed": missed,
        "missed_in_a_row_max": worst_streak,
        "missed_in_a_row_now": streak,
        "meal_adherence_pct": _pct(meals_e, meals_p),
        "off_plan_meals": off_meals,
        "water_compliance_pct": _pct(water_hit, water_days),
        "supplement_adherence_pct": _pct(sup_t, sup_p),
        "last_checkin_status": ck,
        "last_checkin_at": last["created_at"] if last else None,
        "off_plan_days": off_days,
    }


def progress(checkins: list, bloodwork: list) -> dict:
    """Weight trajectory, sleep/stress and photos from weekly check-ins; flags from latest labs."""
    pts = []
    for c in checkins:
        w = _num((c.get("fields") or {}).get("weight_lbs"))
        t = _d(c.get("created_at"))
        if w and t:
            pts.append((t, w))
    pts.sort()
    now = datetime.now(timezone.utc)
    latest = pts[-1] if pts else None
    prev = pts[-2] if len(pts) > 1 else None

    def at_or_before(days):
        cands = [p for p in pts if p[0] <= now - timedelta(days=days)]
        return cands[-1] if cands else None

    wow = None
    if latest and prev:
        span = max((latest[0] - prev[0]).days, 1)
        wow = round((latest[1] - prev[1]) * 7 / span, 2)
    p30 = at_or_before(28)
    recent = checkins[:4]
    sleep = [_num((c.get("fields") or {}).get("sleep_hours")) for c in recent]
    sleep = [s for s in sleep if s]
    scores = {}
    for c in recent:
        for k, v in ((c.get("fields") or {}).get("scores") or {}).items():
            if _num(v) is not None:
                scores.setdefault(k, []).append(float(v))
    photos_30 = sum(len(c.get("photos") or []) for c in checkins if (_d(c.get("created_at")) or now) >= now - timedelta(days=30))
    flags = []
    if bloodwork:
        b = bloodwork[0]
        for m in b.get("markers") or []:
            if m.get("flag") in ("low", "high"):
                flags.append(f"{m.get('name')} {m.get('value')}{(' ' + m['unit']) if m.get('unit') else ''} ({m['flag']})")
    return {
        "weight_latest": {"value": latest[1], "unit": "lbs", "at": latest[0].isoformat()} if latest else None,
        "weight_change_per_week": {"value": wow, "unit": "lbs/week"} if wow is not None else None,
        "weight_change_30d": {"value": round(latest[1] - p30[1], 1), "unit": "lbs"} if latest and p30 else None,
        "weights": [{"at": t.date().isoformat(), "lbs": w} for t, w in pts[-12:]],
        "sleep_avg_hours": round(sum(sleep) / len(sleep), 1) if sleep else None,
        "scores_avg_4wk": {k: round(sum(v) / len(v), 1) for k, v in scores.items()} or None,
        "photo_submissions_30d": photos_30,
        "checkins_total": len(checkins),
        "latest_bloodwork": {"collected_on": bloodwork[0].get("collected_on") or bloodwork[0].get("uploaded_at"),
                             "flagged": flags} if bloodwork else None,
    }


def _rules(c: dict) -> list:
    """Coaching alerts from a client snapshot (Athena's rules)."""
    a, p, out = c["adherence"], c["progress"], []
    name = c["name"]
    if a["missed_in_a_row_now"] >= 3:
        out.append(("high", f"{name} has missed {a['missed_in_a_row_now']} training sessions in a row"))
    elif a["workouts_missed"] >= 3:
        out.append(("medium", f"{name} missed {a['workouts_missed']} workouts in the last 14 days"))
    if a["meal_adherence_pct"] is not None and a["meal_adherence_pct"] < 80:
        out.append(("medium", f"{name}'s meal adherence is {a['meal_adherence_pct']}% (under 80%)"))
    if a["water_compliance_pct"] is not None and a["water_compliance_pct"] < 60:
        out.append(("low", f"{name} hit the water target on only {a['water_compliance_pct']}% of days"))
    if a["supplement_adherence_pct"] is not None and a["supplement_adherence_pct"] < 80:
        out.append(("low", f"{name}'s supplement adherence is {a['supplement_adherence_pct']}%"))
    if a["last_checkin_status"] == "overdue":
        out.append(("medium", f"{name}'s weekly check-in is overdue (last {str(a['last_checkin_at'])[:10]})"))
    elif a["last_checkin_status"] == "pending_review":
        out.append(("info", f"{name} has a check-in waiting for your review"))
    if a["days_logged"] == 0:
        out.append(("medium", f"{name} hasn't logged anything in the app for 14 days"))
    w = p.get("weight_change_per_week")
    if w and abs(w["value"]) > 2.0:
        out.append(("medium", f"{name}'s weight is moving {w['value']:+} lbs/week — check it's intended"))
    s = (p.get("scores_avg_4wk") or {})
    if p.get("sleep_avg_hours") is not None and p["sleep_avg_hours"] < 6:
        out.append(("low", f"{name} is averaging {p['sleep_avg_hours']} h sleep"))
    if s.get("stress") is not None and s["stress"] >= 8:
        out.append(("low", f"{name}'s stress score is averaging {s['stress']}/10"))
    if p.get("latest_bloodwork") and p["latest_bloodwork"]["flagged"]:
        out.append(("info", f"{name}'s latest bloodwork flags: {', '.join(p['latest_bloodwork']['flagged'][:5])}"))
    return out


# ───────────────────────── polling ─────────────────────────
def refresh() -> dict:
    """Pull every client from the FBF app and rebuild snapshots + alerts."""
    with _lock:
        try:
            roster = _get("/coach/clients").get("clients", [])
            clients, alerts = {}, []
            for c in roster:
                try:
                    det = _get(f"/coach/clients/{c['id']}")
                except SessionExpired:
                    raise
                except Exception as e:
                    log.warning("client %s: %s", c.get("id"), e)
                    continue
                snap = {
                    "id": c["id"], "name": c["name"], "email": c.get("email"),
                    "coach": c.get("coach_name"), "app_alerts_7d": c.get("alerts_7d"),
                    "adherence": adherence(det.get("daily", []), det.get("checkins", [])),
                    "progress": progress(det.get("checkins", []), det.get("bloodwork", [])),
                    "latest_checkin": _checkin_brief(det.get("checkins", [])),
                    "recent_events": [{"at": e["created_at"], "kind": e["kind"], "severity": e["severity"], "summary": e["summary"]}
                                      for e in det.get("events", [])[:10]],
                }
                clients[str(c["id"])] = snap
                for sev, msg in _rules(snap):
                    alerts.append({"client_id": c["id"], "client": c["name"], "severity": sev, "message": msg})
            order = {"high": 0, "medium": 1, "low": 2, "info": 3}
            alerts.sort(key=lambda x: (order.get(x["severity"], 9), x["client"]))
            old = {a["message"] for a in _state["alerts"]}
            _state.update(clients=clients, alerts=alerts, updated_at=_now(), error=None,
                          new_alerts=sum(1 for a in alerts if a["message"] not in old and a["severity"] in ("high", "medium")))
            json.dump(_state, open(CACHE_FILE, "w", encoding="utf-8"), indent=2, default=str)
            log.info("FBF refresh: %d clients, %d alerts", len(clients), len(alerts))
        except SessionExpired as e:
            _state["error"] = str(e)
        except Exception as e:
            _state["error"] = f"Refresh failed: {e}"
            log.warning("FBF refresh failed: %s", e)
        return status()


def _checkin_brief(checkins):
    if not checkins:
        return None
    c = checkins[0]
    f = c.get("fields") or {}
    keep = {k: f.get(k) for k in ("weight_lbs", "sleep_hours", "cardio_minutes", "training_week", "scores", "wins", "struggles", "notes", "protocol_notes") if f.get(k) not in (None, "", {})}
    return {"at": c.get("created_at"), "photos": c.get("photos"), "fields": keep, "coach_feedback": c.get("feedback")}


def _load_cache():
    try:
        _state.update(json.load(open(CACHE_FILE, encoding="utf-8")))
    except Exception:
        pass


def _poll_loop():
    _load_cache()
    while True:
        if connected():
            refresh()
        time.sleep(POLL_MIN * 60)


def start():
    threading.Thread(target=_poll_loop, daemon=True, name="fbf-poller").start()


def status() -> dict:
    s = _session()
    return {"connected": bool(s.get("token")), "coach": (s.get("user") or {}).get("name"),
            "clients": len(_state["clients"]), "alerts": len(_state["alerts"]),
            "new_alerts": _state.get("new_alerts", 0), "updated_at": _state["updated_at"],
            "error": _state["error"], "poll_minutes": POLL_MIN}


def clients() -> list:
    out = []
    for c in _state["clients"].values():
        a, p = c["adherence"], c["progress"]
        out.append({"id": c["id"], "name": c["name"], "coach": c.get("coach"),
                    "workouts": f"{a['workouts_completed']} done / {a['workouts_missed']} missed",
                    "meal_pct": a["meal_adherence_pct"], "water_pct": a["water_compliance_pct"],
                    "supp_pct": a["supplement_adherence_pct"], "checkin": a["last_checkin_status"],
                    "last_checkin_at": a["last_checkin_at"],
                    "weight": (p.get("weight_latest") or {}).get("value"),
                    "weight_wk": (p.get("weight_change_per_week") or {}).get("value"),
                    "alerts": sum(1 for x in _state["alerts"] if x["client_id"] == c["id"] and x["severity"] in ("high", "medium"))})
    return sorted(out, key=lambda x: (-x["alerts"], x["name"].lower()))


def client(cid) -> dict | None:
    return _state["clients"].get(str(cid))


def alerts() -> list:
    return _state["alerts"]


# ───────────────────────── Athena ─────────────────────────
_ROSTER_WORDS = re.compile(r"\b(clients?|athletes?|roster|adherence|check-?ins?|compliance|who (?:is|'s) (?:slipping|behind|off)|my people|members?)\b", re.I)


def _match_clients(q: str) -> list:
    ql = q.lower()
    hits = []
    for c in _state["clients"].values():
        parts = [p for p in re.split(r"\s+", c["name"].lower()) if len(p) > 2]
        if c["name"].lower() in ql or any(re.search(rf"\b{re.escape(p)}\b", ql) for p in parts):
            hits.append(c)
    return hits


def athena_context(q: str) -> tuple[str, bool]:
    """(briefing text, is_client_question). Empty text when the question isn't about clients."""
    if not _state["clients"] and not connected():
        if _ROSTER_WORDS.search(q):
            return ("The FBF app isn't connected to Athena yet — Bryan needs to sign in once in the FBF Clients tab.", True)
        return ("", False)
    named = _match_clients(q)
    roster = bool(_ROSTER_WORDS.search(q))
    if not named and not roster:
        return ("", False)
    head = f"LIVE FBF APP DATA (pulled {(_state['updated_at'] or 'never')[:16].replace('T', ' ')} UTC; null = not logged):"
    if _state.get("error"):
        head += f"\nNOTE: last refresh problem — {_state['error']}"
    if named:
        body = "\n\n".join(json.dumps({k: v for k, v in c.items() if k != "email"}, default=str) for c in named[:3])
        al = [a["message"] for a in _state["alerts"] if a["client_id"] in {c["id"] for c in named}]
        return (f"{head}\n{body}\nALERTS: {al or 'none'}", True)
    rows = clients()
    lines = [f"- {r['name']}: workouts {r['workouts']}, meals {r['meal_pct']}%, water {r['water_pct']}%, supps {r['supp_pct']}%, "
             f"check-in {r['checkin']}, weight {r['weight']} lbs ({r['weight_wk']} lbs/wk)" for r in rows]
    al = [f"[{a['severity']}] {a['message']}" for a in _state["alerts"][:25]]
    return (f"{head}\nROSTER (14-day adherence):\n" + "\n".join(lines) + "\nALERTS:\n" + ("\n".join(al) or "none"), True)
