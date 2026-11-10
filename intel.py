"""
Intel tools for the FBF Command Center.
  supplier_check(domain, phone)  -> due diligence on a peptide supplier (a business), with red flags
  peptide_trends(terms)          -> world interest / news / research / trial / Reddit trends per compound
All lookups are about businesses and products, never private individuals.
"""
import datetime as dt, json, re, socket, ssl, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
UA = {"User-Agent": "FBF-CommandCenter/1.0 (forgedbyfreedom.net)"}
_cache = {}


def _get(url, timeout=20, headers=None, raw=False):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return b if raw else json.loads(b)


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ─────────────────────────── supplier check ───────────────────────────
PRIVACY = re.compile(r"privacy|redacted|proxy|whoisguard|withheld|domains by proxy|perfect privacy|njalla|contact privacy", re.I)
OFFSHORE_TLD = {".ru", ".su", ".cn", ".top", ".xyz", ".shop", ".store", ".site", ".online", ".icu", ".cc", ".to", ".is"}


def _clean_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0]
    return d[4:] if d.startswith("www.") else d


def _rdap(domain):
    out = {}
    try:
        j = _get(f"https://rdap.org/domain/{urllib.parse.quote(domain)}")
    except Exception as e:
        return {"error": f"RDAP lookup failed: {e}"}
    for ev in j.get("events", []):
        a = ev.get("eventAction"); d = (ev.get("eventDate") or "")[:10]
        if a == "registration": out["registered"] = d
        elif a == "expiration": out["expires"] = d
        elif a == "last changed": out["updated"] = d
    names = []
    for ent in j.get("entities", []):
        roles = ent.get("roles", [])
        vc = ent.get("vcardArray", [None, []])[1] if ent.get("vcardArray") else []
        fn = next((v[3] for v in vc if v and v[0] in ("fn", "org")), "")
        if "registrar" in roles:
            out["registrar"] = fn
        if "registrant" in roles:
            names.append(fn)
    out["registrant"] = names[0] if names else ""
    out["privacy_protected"] = (not names) or bool(PRIVACY.search(" ".join(names)))
    out["status"] = j.get("status", [])
    if out.get("registered"):
        try:
            out["age_days"] = (dt.date.today() - dt.date.fromisoformat(out["registered"])).days
        except ValueError:
            pass
    return out


def _dns(domain):
    import dns.resolver
    r = dns.resolver.Resolver(); r.timeout = r.lifetime = 6
    out = {}
    def q(name, t):
        try:
            return [x.to_text().strip('"') for x in r.resolve(name, t)]
        except Exception:
            return []
    out["a"] = q(domain, "A")[:4]
    out["mx"] = q(domain, "MX")[:4]
    txt = q(domain, "TXT")
    out["spf"] = any(t.startswith("v=spf1") for t in txt)
    out["dmarc"] = any(t.startswith("v=DMARC1") for t in q("_dmarc." + domain, "TXT"))
    ns = q(domain, "NS"); out["nameservers"] = ns[:4]
    out["cloudflare"] = any("cloudflare" in n for n in ns)
    return out


def _ssl(domain):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=8) as s, ctx.wrap_socket(s, server_hostname=domain) as t:
            c = t.getpeercert()
        issuer = dict(x[0] for x in c.get("issuer", []))
        nb = dt.datetime.strptime(c["notBefore"], "%b %d %H:%M:%S %Y %Z").date()
        na = dt.datetime.strptime(c["notAfter"], "%b %d %H:%M:%S %Y %Z").date()
        return {"valid": True, "issuer": issuer.get("organizationName", ""), "from": str(nb), "to": str(na)}
    except Exception as e:
        return {"valid": False, "error": str(e)[:120]}


def _wayback(domain):
    try:
        j = _get(f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&limit=1&fl=timestamp&filter=statuscode:200", timeout=25)
        first = j[1][0] if len(j) > 1 else None
        j2 = _get(f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&limit=-1&fl=timestamp", timeout=25)
        last = j2[-1][0] if len(j2) > 1 else None
        f = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}" if s else None
        return {"first_seen": f(first), "last_seen": f(last)}
    except Exception as e:
        return {"error": str(e)[:120]}


def _phone(phone, secret):
    import phonenumbers
    from phonenumbers import carrier, geocoder, number_type, PhoneNumberType as T
    out = {"input": phone}
    try:
        n = phonenumbers.parse(phone, "US")
    except Exception:
        return {"input": phone, "valid": False, "error": "Not a phone number"}
    out["valid"] = phonenumbers.is_valid_number(n)
    out["e164"] = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    out["country"] = phonenumbers.region_code_for_number(n)
    out["region"] = geocoder.description_for_number(n, "en")
    types = {T.MOBILE: "mobile", T.FIXED_LINE: "landline", T.FIXED_LINE_OR_MOBILE: "landline or mobile",
             T.VOIP: "voip", T.TOLL_FREE: "toll-free", T.PREMIUM_RATE: "premium-rate"}
    out["type_offline"] = types.get(number_type(n), "unknown")
    out["carrier_offline"] = carrier.name_for_number(n, "en")
    sid, tok = secret("TWILIO_ACCOUNT_SID"), secret("TWILIO_AUTH_TOKEN")
    if sid and tok and out["valid"]:
        import base64
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        try:
            j = _get(f"https://lookups.twilio.com/v2/PhoneNumbers/{urllib.parse.quote(out['e164'])}"
                     "?Fields=line_type_intelligence,caller_name", headers={"Authorization": "Basic " + auth})
            lti = j.get("line_type_intelligence") or {}
            cn = j.get("caller_name") or {}
            out["line_type"] = lti.get("type")            # mobile, landline, nonFixedVoip, fixedVoip, tollFree...
            out["carrier"] = lti.get("carrier_name")
            out["caller_name"] = cn.get("caller_name")     # registered CNAM (business name for business lines)
            out["caller_type"] = cn.get("caller_type")     # BUSINESS / CONSUMER
            out["source"] = "Twilio Lookup"
        except Exception as e:
            out["twilio_error"] = str(e)[:120]
    else:
        out["source"] = "offline number plan (add Twilio keys for live carrier/line type)"
    return out


def _fda_hits(names):
    """Match firm/product names against FDA's Health Fraud Product Database (pulled nightly by the research job)."""
    p = Path(r"C:\AI\scholar-ingest\data\fda_raw\tainted_db.html")
    if not p.exists() or not names:
        return []
    h = p.read_text(encoding="utf-8", errors="replace")
    rows = re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", h)
    hits = []
    for r in rows:
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)).strip()
        for nm in names:
            if nm and len(nm) >= 4 and nm.lower() in txt.lower():
                hits.append(txt[:220]); break
    return hits[:10]


def supplier_check(domain="", phone="", name="", secret=lambda k: None):
    res = {"checked": dt.datetime.now().isoformat(timespec="seconds"), "flags": [], "good": []}
    d = _clean_domain(domain)
    if d:
        res["domain"] = d
        res["whois"] = _rdap(d)
        res["dns"] = _dns(d)
        res["ssl"] = _ssl(d)
        res["archive"] = _wayback(d)
        w, n_, s, a = res["whois"], res["dns"], res["ssl"], res["archive"]
        age = w.get("age_days")
        if age is not None:
            (res["flags"] if age < 365 else res["good"]).append(
                f"Domain registered {w.get('registered')} ({age // 365}y {age % 365 // 30}m old)" + (" — under a year old" if age < 365 else ""))
        if w.get("privacy_protected"):
            res["flags"].append("Owner hidden behind a privacy service (no named company on the registration)")
        elif w.get("registrant"):
            res["good"].append(f"Registered to: {w['registrant']}")
        if w.get("expires"):
            try:
                if (dt.date.fromisoformat(w["expires"]) - dt.date.today()).days < 120:
                    res["flags"].append(f"Domain only paid through {w['expires']} (short-term registration)")
            except ValueError:
                pass
        if "." + d.rsplit(".", 1)[-1] in OFFSHORE_TLD:
            res["flags"].append(f"High-risk domain ending (.{d.rsplit('.', 1)[-1]})")
        if not n_.get("mx"):
            res["flags"].append("No email server on the domain (no business email)")
        if not n_.get("spf") or not n_.get("dmarc"):
            res["flags"].append("Email authentication incomplete (missing " + ", ".join(x for x, ok in (("SPF", n_.get("spf")), ("DMARC", n_.get("dmarc"))) if not ok) + ")")
        if not s.get("valid"):
            res["flags"].append("No valid HTTPS certificate")
        if a.get("first_seen"):
            (res["good"] if a["first_seen"] < str(dt.date.today().year - 1) else res["flags"]).append(
                f"First archived {a['first_seen']}, last {a.get('last_seen')}")
        elif not a.get("error"):
            res["flags"].append("Never captured by the Internet Archive")
    if phone:
        p = res["phone"] = _phone(phone, secret)
        lt = (p.get("line_type") or p.get("type_offline") or "").lower()
        if not p.get("valid"):
            res["flags"].append("Phone number is not a valid number")
        elif "voip" in lt.replace("nonfixed", "voip"):
            res["flags"].append(f"Phone is a VoIP line ({p.get('carrier') or p.get('carrier_offline') or 'internet carrier'}) — common for burner/sales numbers")
        elif lt:
            res["good"].append(f"Phone: {lt} line" + (f" via {p.get('carrier')}" if p.get("carrier") else ""))
        if p.get("caller_name"):
            res["good"].append(f"Registered caller name: {p['caller_name']} ({p.get('caller_type') or 'type unknown'})")
        if p.get("country") and p["country"] not in ("US", "CA") :
            res["flags"].append(f"Number registered outside North America ({p['country']}, {p.get('region') or ''})")
    names = [x for x in {name.strip(), d.split(".")[0] if d else ""} if x]
    res["fda_health_fraud_hits"] = _fda_hits(names)
    if res["fda_health_fraud_hits"]:
        res["flags"].insert(0, f"Appears in FDA's Health Fraud database ({len(res['fda_health_fraud_hits'])} entries)")
    nf = len(res["flags"])
    res["risk"] = "HIGH" if nf >= 4 or res["fda_health_fraud_hits"] else "MEDIUM" if nf >= 2 else "LOW"
    res["note"] = ("Checks the business's public footprint only. Always verify product purity with an independent "
                   "third-party lab COA tied to the batch number before trusting a supplier.")
    return res


# ─────────────────────────── peptide trends ───────────────────────────
DEFAULT_TERMS = ["retatrutide", "tirzepatide", "semaglutide", "BPC-157", "TB-500", "GHK-Cu", "tesamorelin",
                 "ipamorelin", "MOTS-c", "cagrilintide", "NAD+", "SS-31"]


def _gtrends(terms):
    """Worldwide Google search interest, last 12 months (0-100, comparable within each batch of 5)."""
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=300, timeout=(10, 25))
        out = {}
        anchor = terms[0]
        for i in range(0, len(terms), 4):
            batch = [anchor] + [t for t in terms[i:i + 4] if t != anchor]
            pt.build_payload(batch, timeframe="today 12-m", geo="")
            df = pt.interest_over_time()
            if df is None or df.empty:
                continue
            for t in batch:
                if t in df:
                    s = [int(v) for v in df[t].tolist()]
                    out[t] = {"series": s, "dates": [d.strftime("%Y-%m-%d") for d in df.index],
                              "last4": round(sum(s[-4:]) / 4, 1), "prev4": round(sum(s[-8:-4]) / 4, 1)}
            time.sleep(1.5)
        return out
    except Exception as e:
        return {"_error": f"Google Trends unavailable: {str(e)[:120]}"}


_gdelt_last = [0.0]


def _gdelt(term):
    """Worldwide news articles mentioning the term, daily volume for 90 days (GDELT allows 1 request / 5 s)."""
    try:
        q = urllib.parse.quote(f'"{term}"' if re.search(r"[\s-]", term) else term)
        for attempt in range(3):
            wait = 5.5 - (time.time() - _gdelt_last[0])
            if wait > 0:
                time.sleep(wait)
            _gdelt_last[0] = time.time()
            try:
                raw = _get(f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=timelinevolraw&timespan=90d&format=json", timeout=30, raw=True)
                j = json.loads(raw)
                break
            except (ValueError, urllib.error.HTTPError):
                if attempt == 2:
                    raise
                time.sleep(6)
        pts = (j.get("timeline") or [{}])[0].get("data", [])
        vals = [int(p.get("value", 0)) for p in pts]
        return {"series": vals, "last30": sum(vals[-30:]), "prev30": sum(vals[-60:-30])}
    except Exception as e:
        return {"error": str(e)[:100]}


def _pubmed(term):
    try:
        y = dt.date.today().year
        cnt = lambda y_: int(_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&retmode=json&rettype=count&term="
                                  + urllib.parse.quote(f'"{term}"[tiab] AND {y_}[dp]'))["esearchresult"]["count"])
        return {"this_year": cnt(y), "last_year": cnt(y - 1)}
    except Exception as e:
        return {"error": str(e)[:100]}


def _trials(term):
    try:
        since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
        j = _get("https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode(
            {"query.term": term, "filter.advanced": f"AREA[StudyFirstPostDate]RANGE[{since},MAX]", "countTotal": "true", "pageSize": 1}))
        return {"new_12mo": j.get("totalCount", 0)}
    except Exception as e:
        return {"error": str(e)[:100]}


def _reddit(term):
    """Public Reddit posts mentioning the term in the past week/month (aggregate counts only)."""
    try:
        def n(t):
            j = _get("https://www.reddit.com/search.json?" + urllib.parse.urlencode({"q": f'"{term}"', "t": t, "limit": 100, "sort": "new"}),
                     headers={"User-Agent": "FBF-CommandCenter/1.0 trend-counter"})
            return len(j.get("data", {}).get("children", []))
        return {"week": n("week"), "month": n("month")}
    except Exception as e:
        return {"error": str(e)[:100]}


def peptide_trends(terms=None, refresh=False):
    terms = [t.strip() for t in (terms or DEFAULT_TERMS) if t.strip()][:16]
    key = "trends:" + "|".join(terms)
    if refresh:
        _cache.pop(key, None)

    def build():
        g = _gtrends(terms)
        rows = []
        for t in terms:
            rows.append({"term": t, "google": g.get(t), "news": _gdelt(t), "pubmed": _pubmed(t),
                         "trials": _trials(t)})
            time.sleep(0.4)
        for r in rows:  # momentum: % change, last 4 weeks vs the 4 before (Google) and last 30d vs prior 30d (news)
            go, nw = r.get("google") or {}, r.get("news") or {}
            pct = lambda a, b: round((a - b) / b * 100) if b else (100 if a else 0)
            r["momentum_search"] = pct(go.get("last4", 0), go.get("prev4", 0)) if go else None
            r["momentum_news"] = pct(nw.get("last30", 0), nw.get("prev30", 0)) if "last30" in nw else None
        rows.sort(key=lambda r: (r["momentum_search"] or 0) + (r["momentum_news"] or 0) / 2, reverse=True)
        out = {"updated": dt.datetime.now().isoformat(timespec="seconds"), "terms": rows, "google_error": g.get("_error")}
        DATA.mkdir(exist_ok=True)
        (DATA / "peptide_trends.json").write_text(json.dumps(out))
        return out
    return _cached(key, 6 * 3600, build)


# ─────────────────────────── contact verifier (anti-impersonation) ───────────────────────────
# Bryan keeps a registry of each supplier rep's REAL contact points. Any new number/handle is checked
# against it: exact match = verified; near-miss = impersonation red flag; listed scammer = block.
CONTACTS = DATA / "verified_contacts.json"
CHANNELS = ("whatsapp", "telegram", "instagram", "wechat", "phone", "email", "website", "signal", "facebook", "x")
HOMO = str.maketrans({"0": "o", "1": "l", "i": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a", "$": "s",
                      "|": "l", "!": "l"})


def _load_contacts():
    try:
        return json.loads(CONTACTS.read_text())
    except Exception:
        return {"verified": [], "scam": []}


def _save_contacts(c):
    DATA.mkdir(exist_ok=True)
    CONTACTS.write_text(json.dumps(c, indent=1))


def _norm(channel, v):
    v = (v or "").strip()
    if channel in ("whatsapp", "phone", "signal"):
        return re.sub(r"\D", "", v)
    if channel == "email":
        return v.lower()
    if channel == "website":
        return _clean_domain(v)
    v = re.sub(r"^https?://(www\.)?(t\.me|telegram\.me|instagram\.com|facebook\.com|x\.com|twitter\.com)/", "", v.lower())
    return v.lstrip("@").split("/")[0].split("?")[0]


def _skeleton(s):
    import unicodedata
    s = unicodedata.normalize("NFKC", s).lower().replace("rn", "m").replace("vv", "w").replace("cl", "d")
    s = s.translate(HOMO)
    return re.sub(r"[\W_]+", "", s)


def _lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def contacts_list():
    return _load_contacts()


def contacts_add(kind, person, company, channel, value, note=""):
    if kind not in ("verified", "scam") or channel not in CHANNELS or not value:
        raise ValueError("kind must be verified|scam, channel one of " + ", ".join(CHANNELS))
    c = _load_contacts()
    c[kind].append({"person": person, "company": company, "channel": channel, "value": value.strip(),
                    "norm": _norm(channel, value), "note": note, "added": dt.date.today().isoformat()})
    _save_contacts(c)
    return c


def contacts_remove(kind, idx):
    c = _load_contacts()
    if 0 <= idx < len(c.get(kind, [])):
        c[kind].pop(idx)
        _save_contacts(c)
    return c


def _telegram_public(user):
    """Public t.me preview: does the username exist, and what display name does it show."""
    try:
        h = _get(f"https://t.me/{urllib.parse.quote(user)}", raw=True, timeout=15).decode("utf-8", "replace")
        title = re.search(r'<div class="tgme_page_title"[^>]*><span[^>]*>(.*?)</span>', h, re.S)
        extra = re.search(r'<div class="tgme_page_extra">(.*?)</div>', h, re.S)
        if not title:
            return {"exists": False}
        clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()
        return {"exists": True, "display_name": clean(title.group(1)), "type": clean(extra.group(1)) if extra else ""}
    except Exception as e:
        return {"error": str(e)[:100]}


def contact_check(channel, value, claimed_person="", secret=lambda k: None):
    channel = channel.lower()
    n = _norm(channel, value)
    c = _load_contacts()
    res = {"channel": channel, "value": value, "normalized": n, "verdict": "UNKNOWN", "reasons": [], "matches": []}
    same_ch = [x for x in c["verified"] if x["channel"] == channel or
               (x["channel"] in ("whatsapp", "phone", "signal") and channel in ("whatsapp", "phone", "signal"))]
    # 1. known scammer
    for s in c["scam"]:
        if s["norm"] == n:
            res["verdict"] = "SCAM"; res["reasons"].append(f"On your known-scammer list ({s.get('note') or 'flagged ' + s['added']})")
            res["matches"].append(s)
    # 2. exact verified match
    for v in same_ch:
        if v["norm"] == n and res["verdict"] != "SCAM":
            res["verdict"] = "VERIFIED"; res["reasons"].append(f"Exact match to {v['person']} ({v['company']}) — verified {v['added']}")
            res["matches"].append(v)
    # 3. lookalikes of a verified contact (impersonation)
    if res["verdict"] == "UNKNOWN":
        for v in same_ch:
            if channel in ("whatsapp", "phone", "signal"):
                if n[-7:] and n[-7:] == v["norm"][-7:] and n != v["norm"]:
                    res["reasons"].append(f"Same last 7 digits as {v['person']}'s real number but a different number")
                    res["matches"].append(v)
                continue
            a, b = _skeleton(n), _skeleton(v["norm"])
            d = _lev(a, b)
            if a == b or d <= 2 or (len(b) >= 4 and (b in a or a in b)):
                res["reasons"].append(f"Lookalike of {v['person']}'s real {v['channel']} '{v['value']}' "
                                      f"({'identical after removing look-alike characters' if a == b else f'{d} character(s) different' if d <= 2 else 'contains her real handle'})")
                res["matches"].append(v)
        if res["matches"]:
            res["verdict"] = "IMPERSONATION RISK"
    # 4. claims to be someone you have verified, but this isn't one of their contact points
    if claimed_person and res["verdict"] not in ("VERIFIED", "SCAM"):
        known = [v for v in c["verified"] if v["person"].lower() == claimed_person.strip().lower()]
        if known:
            res["verdict"] = "IMPERSONATION RISK"
            res["reasons"].append(f"Claims to be {claimed_person}, but this {channel} isn't one of their verified contacts: "
                                  + "; ".join(f"{k['channel']} {k['value']}" for k in known))
    # 5. channel-specific signals
    if channel in ("whatsapp", "phone", "signal") and n:
        p = res["phone"] = _phone("+" + n if not value.strip().startswith("+") and len(n) > 10 else value, secret)
        lt = (p.get("line_type") or p.get("type_offline") or "").lower()
        if "voip" in lt:
            res["reasons"].append(f"VoIP / internet number ({p.get('carrier') or p.get('carrier_offline') or 'unknown carrier'}) — easy to create and discard")
        if p.get("country"):
            res["reasons"].append(f"Registered country: {p['country']} {p.get('region') or ''}".strip())
    if channel == "telegram" and n:
        t = res["telegram"] = _telegram_public(n)
        if t.get("exists") is False:
            res["reasons"].append("This Telegram username does not exist (or was deleted)")
        elif t.get("display_name"):
            res["reasons"].append(f"Telegram shows display name: {t['display_name']}")
    if channel == "website" and n:
        res["site"] = supplier_check(domain=n, secret=secret)
        res["reasons"] += res["site"]["flags"][:4]
    res["advice"] = {
        "VERIFIED": "Matches your verified contact. Still confirm any change in payment details by voice on the number you already trust.",
        "SCAM": "Known scammer. Do not pay or share anything.",
        "IMPERSONATION RISK": "Treat as fake until confirmed. Message the real contact on the channel you already verified and ask if this is them.",
        "UNKNOWN": "Not in your verified list. Confirm through a channel you already trust (the company's official site or your existing chat) before paying.",
    }[res["verdict"]]
    return res


# ─────────────────────────── batch / COA check (counterfeit product) ───────────────────────────
LAB_VERIFY = {  # labs whose site lets a buyer look up a report directly (add more as you confirm them)
    "janoshik": "https://janoshik.com/",
}


def batch_add(vendor, compound, batch, lab="", report_id="", purity="", note=""):
    c = _load_contacts()
    c.setdefault("batches", []).append({"vendor": vendor.strip(), "compound": compound.strip(), "batch": batch.strip(),
                                        "lab": lab.strip(), "report_id": report_id.strip(), "purity": purity.strip(),
                                        "note": note, "added": dt.date.today().isoformat()})
    _save_contacts(c)
    return c


def batch_check(vendor, batch, compound="", lab="", report_id=""):
    c = _load_contacts()
    bn = re.sub(r"[\W_]+", "", batch.lower())
    known = [b for b in c.get("batches", []) if b["vendor"].lower() == vendor.strip().lower()]
    res = {"vendor": vendor, "batch": batch, "verdict": "UNVERIFIED", "reasons": [], "match": None}
    exact = [b for b in known if re.sub(r"[\W_]+", "", b["batch"].lower()) == bn]
    if exact:
        b = exact[0]; res["match"] = b
        res["verdict"] = "MATCHES A GENUINE BATCH"
        res["reasons"].append(f"Batch {b['batch']} ({b['compound']}) was recorded from {b['vendor']} on {b['added']}"
                              + (f", tested by {b['lab']} #{b['report_id']}" if b.get("lab") else "") + (f", {b['purity']}" if b.get("purity") else ""))
        if compound and b["compound"] and compound.lower() not in b["compound"].lower():
            res["verdict"] = "MISMATCH"; res["reasons"].append(f"But that batch number belongs to {b['compound']}, not {compound}")
        if report_id and b.get("report_id") and re.sub(r"\W", "", report_id) != re.sub(r"\W", "", b["report_id"]):
            res["verdict"] = "MISMATCH"; res["reasons"].append(f"Lab report number differs from the genuine one ({b['report_id']})")
    else:
        close = [b for b in known if _lev(re.sub(r"[\W_]+", "", b["batch"].lower()), bn) <= 2]
        if close:
            res["verdict"] = "SUSPICIOUS"
            res["reasons"].append("Not a recorded genuine batch, but close to: " + ", ".join(b["batch"] for b in close) + " — a common counterfeit tactic")
        elif known:
            res["reasons"].append(f"Not among the {len(known)} genuine {vendor} batches you've recorded")
        else:
            res["reasons"].append(f"No genuine batches recorded for {vendor} yet — add the ones you receive directly")
    if lab:
        url = next((u for k, u in LAB_VERIFY.items() if k in lab.lower()), None)
        res["verify_with_lab"] = url
        res["reasons"].append(f"Confirm report #{report_id or '?'} directly on the lab's site, not a screenshot: {url}" if url
                              else "Confirm the report directly with the testing lab, never from a screenshot or PDF the seller sends")
    res["advice"] = ("A real lab report proves one vial was tested, not that yours came from the same batch. Buy only through "
                     "the vendor's verified contact, and consider testing your own vial.")
    return res
