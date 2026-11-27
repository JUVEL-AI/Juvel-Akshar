"""Lead sheets chosen on the dashboard: which Google Sheets to read, which tab, and which column is which field.
Several sheets can be active. The first read of a newly added sheet only records what is already in it (nobody
who enquired before go-live is called); after that every new row is an enquiry."""
from __future__ import annotations
import json
import re
from datetime import datetime

from .store import iso
from .normalize import clean_phone

FIELDS = [  # key, label, required
    ("phone", "Parent phone", True), ("name", "Parent name", True), ("campus", "Campus", False),
    ("grade", "Class / grade", False), ("grade_alt", "Class / grade (backup column, if parents sometimes fill the wrong one)", False),
    ("student_name", "Student name", False), ("created_at", "Time of enquiry", True),
    ("email", "Email", False), ("city", "City", False)]
REQUIRED = [k for k, _, r in FIELDS if r]
ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


DEFAULT_SKIP_WORDS = ["not qualified", "unqualified", "not interested", "admitted", "wrong number", "do not call", "dnd"]


def _as_list(v, limit: int, width: int) -> list[str]:
    items = v if isinstance(v, list) else str(v or "").split(",")
    out = []
    for x in items:
        x = re.sub(r"\s+", " ", str(x)).strip()[:width]
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:limit]


def _skip_reason(row: dict, cols: list[str], words: list[str]) -> str:
    """The school already worked on this parent when one of the chosen columns says one of the skip words."""
    want = {re.sub(r"\s+", " ", c).strip().lower() for c in cols}
    for h, v in row.items():
        if re.sub(r"\s+", " ", str(h)).strip().lower() in want:
            t = str(v or "").lower()
            if any(w and w in t for w in words):
                return f"{str(h).strip()} says '{str(v).strip()[:40]}'"
    return ""


def year_start(v) -> int | None:
    """First year of an academic year as written in a sheet: '2027-2028', '2027-28', '2027 - 2028', '2027' all give 2027."""
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(v or ""))
    return int(m.group(1)) if m else None


def _year_reason(row: dict, col: str, allowed: str) -> str:
    """Closed when the parent is applying for an academic year earlier than the allowed one. A blank or unreadable year is called."""
    want, base = year_start(allowed), re.sub(r"\s+", " ", col).strip().lower()
    if not (want and base):
        return ""
    for h, v in row.items():                              # a repeated heading is read as 'Name.1': the first filled one wins
        if re.sub(r"\.\d+$", "", re.sub(r"\s+", " ", str(h)).strip()).lower() == base and str(v or "").strip():
            y = year_start(v)
            if y is not None and y < want:
                return f"applying for academic year {str(v).strip()[:12]}, not {allowed}"
            return ""
    return ""


def parse_sheet_id(text: str) -> str:
    t = (text or "").strip()
    m = ID_RE.search(t)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", t):
        return t
    raise ValueError("That is not a Google Sheets link. Paste the address from the browser, which contains /d/…/edit")


# ---------------- credentials ----------------
def google_json(engine) -> str:
    import os
    try:
        saved = json.loads(engine.store.kv_get("integrations") or "{}").get("google_service_account", "")
    except ValueError:
        saved = ""
    return saved or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def service_email(creds: str) -> str:
    try:
        return str(json.loads(creds).get("client_email", ""))
    except Exception:
        return ""


def validate_google_json(text: str) -> str:
    try:
        d = json.loads(text)
    except ValueError:
        raise ValueError("Google key: paste the whole JSON file contents")
    if d.get("type") != "service_account" or not d.get("client_email") or not d.get("private_key"):
        raise ValueError("Google key: this is not a service account key file")
    return json.dumps(d)


# ---------------- column guessing ----------------
def suggest_columns(headers: list[str]) -> dict:
    """Best guess of which column is which field, from the column names. The admin confirms it."""
    h = [(x, re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()) for x in headers]

    def pick(pred, avoid=()):
        for raw, n in h:
            if n and pred(n) and not any(a in n for a in avoid):
                return raw
        return ""
    out = {
        "phone": pick(lambda n: any(w in n for w in ("phone", "mobile", "whatsapp", "contact number", "contact no", "contactno"))),
        "campus": pick(lambda n: "campus" in n or "branch" in n),
        "grade": pick(lambda n: any(w in n for w in ("admission", "applying", "sought")) and any(w in n for w in ("grade", "class", "sought")), avoid=("school",))
        or pick(lambda n: any(w in n for w in ("grade", "class", "standard", "std")), avoid=("school", "campaign")),
        "grade_alt": "",
        "email": pick(lambda n: "email" in n or "e mail" in n),
        "city": pick(lambda n: n in ("city", "location", "area") or n.endswith(" city")),
        "created_at": pick(lambda n: any(w in n for w in ("timing", "timestamp", "created", "submitted", "date", "time")) and "willing" not in n),
        "student_name": pick(lambda n: any(w in n for w in ("student", "child")) and "name" in n, avoid=("school", "grade", "class")),
    }
    # Meta's form: one question asks the school, one the grade; answers often swap. Offer the other as the backup.
    school_q = pick(lambda n: "school" in n and "currently" in n)
    grade_q = pick(lambda n: "grade" in n and "currently" in n)
    if school_q and grade_q:
        out["grade"], out["grade_alt"] = grade_q, school_q
    out["name"] = pick(lambda n: n in ("full name", "parent name", "name") or n.startswith("full name")) \
        or pick(lambda n: "name" in n, avoid=("student", "child", "ad ", "adset", "campaign", "school", "sheet", "profile", "form")) \
        or pick(lambda n: n in ("name", "parent", "parent name"))
    return out


# ---------------- reading ----------------
def _client(creds: str):
    import gspread
    return gspread.service_account_from_dict(json.loads(creds))


def _friendly(e: Exception, creds: str) -> str:
    s = str(e)
    em = service_email(creds)
    if "PERMISSION" in s.upper() or "403" in s:
        return f"The system cannot open this sheet. In Google Sheets press Share and add {em or 'the service account address'} as a Viewer."
    if "404" in s or "NOT FOUND" in s.upper() or "SpreadsheetNotFound" in type(e).__name__:
        return "Google could not find that sheet. Check the link, and that it is shared with " + (em or "the service account address") + "."
    if "WorksheetNotFound" in type(e).__name__:
        return "That tab name does not exist in the sheet. Check the spelling of the tab at the bottom of the sheet."
    return "Could not read the sheet: " + s[:160]


def preview(engine, spreadsheet: str, worksheet: str, saved_cols: dict | None = None) -> dict:
    creds = google_json(engine)
    if not creds:
        raise ValueError("Add the Google service account key on the Connections page first.")
    sid = parse_sheet_id(spreadsheet)
    try:
        sh = _client(creds).open_by_key(sid)
        tabs = [w.title for w in sh.worksheets()]
        ws = sh.worksheet(worksheet) if worksheet else sh.get_worksheet(0)
        vals = ws.get_all_values()
    except Exception as e:
        raise ValueError(_friendly(e, creds))
    headers = [str(x).strip() for x in (vals[0] if vals else [])]
    rows = [r for r in vals[1:] if any(c.strip() for c in r)]
    sample = []
    for r in rows[:3]:
        d = {}
        for i, hname in enumerate(headers):
            v = r[i] if i < len(r) else ""
            if hname and any(w in hname.lower() for w in ("phone", "mobile", "email")):
                v = (v[:3] + "•••" + v[-3:]) if len(v) > 8 else v
            if hname:
                d[hname] = v
        sample.append(d)
    return {"spreadsheet_id": sid, "title": sh.title, "tabs": tabs, "worksheet": ws.title, "headers": [x for x in headers if x],
            "row_count": len(rows), "sample": sample, "suggested": suggest_columns(headers)}


def read_rows(creds: str, spreadsheet_id: str, worksheet: str) -> list[dict]:
    ws = _client(creds).open_by_key(spreadsheet_id).worksheet(worksheet)
    vals = ws.get_all_values()
    if not vals:
        return []
    headers, seen = [], {}
    for h in vals[0]:
        h = str(h).strip()
        n = seen.get(h, 0)
        seen[h] = n + 1
        headers.append(h if n == 0 else f"{h}.{n}")
    return [{headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers)) if headers[i]} for r in vals[1:]]


# ---------------- stored settings ----------------
def load(engine) -> dict:
    try:
        d = json.loads(engine.store.kv_get("sheets") or "{}")
    except ValueError:
        d = {}
    d.setdefault("sheets", [])
    return d


def configured(engine) -> list[dict]:
    """Sheets chosen on the dashboard; if none, the one from the server settings (SHEET_ID), as before."""
    saved = load(engine)["sheets"]
    if saved:
        return [s for s in saved if s.get("enabled", True)]
    sc = (engine.cfg.sources or {}).get("sheet") or {}
    if sc.get("spreadsheet_id"):
        return [{"id": "server", "label": "Server setting", "spreadsheet_id": sc["spreadsheet_id"],
                 "worksheet": sc.get("worksheet", "Sheet1"), "columns": engine.cfg.sources.get("csv_columns", {}), "enabled": True}]
    return []


def validate(engine, payload: dict) -> dict:
    out = {"sheets": []}
    for i, s in enumerate(payload.get("sheets") or []):
        label = str(s.get("label") or f"Sheet {i + 1}").strip()[:40]
        sid = parse_sheet_id(s.get("spreadsheet") or s.get("spreadsheet_id") or "")
        ws = str(s.get("worksheet") or "").strip()[:80]
        if not ws:
            raise ValueError(f"{label}: enter the tab name")
        cols = {k: str(v).strip() for k, v in (s.get("columns") or {}).items() if k in dict((k, 1) for k, _, _ in FIELDS) and str(v).strip()}
        if s.get("enabled", True):
            missing = [lab for k, lab, req in FIELDS if req and not cols.get(k)]
            if missing:
                raise ValueError(f"{label}: choose the column for " + ", ".join(missing))
        sf = str(s.get("start_from") or "").strip()
        if sf:
            try:
                datetime.strptime(sf, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{label}: the start date must look like 2026-11-01") from None
        bf, bt = (str(s.get("backfill_from") or "").strip(), str(s.get("backfill_to") or "").strip())
        if bool(bf) != bool(bt):
            raise ValueError(f"{label}: fill both dates of 'also call older enquiries', or neither")
        if bf:
            try:
                a, b = datetime.strptime(bf, "%Y-%m-%d"), datetime.strptime(bt, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{label}: dates must look like 2026-05-01") from None
            if a > b:
                raise ValueError(f"{label}: the older-enquiries 'from' date is after its 'to' date")
        sk_cols, sk_words = _as_list(s.get("skip_columns"), 6, 60), [w.lower() for w in _as_list(s.get("skip_words"), 30, 40)]
        yc, ay = str(s.get("year_column") or "").strip()[:60], str(s.get("allowed_year") or "").strip()[:20]
        if bool(yc) != bool(ay):
            raise ValueError(f"{label}: fill both the academic-year column and the year to call (for example 2027-2028), or neither")
        if ay and year_start(ay) is None:
            raise ValueError(f"{label}: the academic year must look like 2027-2028")
        out["sheets"].append({"id": str(s.get("id") or sid[:8] + str(i)), "label": label, "spreadsheet_id": sid,
                              "worksheet": ws, "columns": cols, "enabled": bool(s.get("enabled", True)), "start_from": sf,
                              "backfill_from": bf, "backfill_to": bt, "skip_columns": sk_cols, "skip_words": sk_words,
                              "year_column": yc, "allowed_year": ay})
    dc = str(payload.get("default_campus", "") or "").strip()
    if dc and dc not in engine._base["campuses"]:
        raise ValueError("Unknown default campus")
    out["default_campus"] = dc
    do = str(payload.get("date_order") or "mdy")
    if do not in ("mdy", "dmy"):
        raise ValueError("Date order must be month first or day first")
    out["date_order"] = do
    return out


def save(engine, payload: dict, now: datetime, who: str) -> dict:
    from . import ops
    clean = validate(engine, payload)
    engine.store.kv_set("sheets", json.dumps(clean))
    engine.store.add_audit(now, who, "sheets", f"{len(clean['sheets'])} sheet(s)")
    ops.apply_overrides(engine)
    return view(engine)


def view(engine) -> dict:
    d = load(engine)
    creds = google_json(engine)
    sc = engine.cfg.sources or {}
    def _st(sid):
        try:
            return json.loads(engine.store.kv_get("sheet_status_" + sid) or "null")
        except Exception:
            return None
    return {"sheets": [{**s, "status": _st(s.get("id", ""))} for s in d["sheets"]], "service_email": service_email(creds), "has_credentials": bool(creds),
            "default_campus": sc.get("default_campus", "") or "", "date_order": sc.get("date_order", "mdy"),
            "presets": [{**p, "spreadsheet": p["spreadsheet_id"]} for p in sc.get("presets", [])],
            "from_server": bool(not d["sheets"] and configured(engine)),
            "campuses": {k: v["label"] for k, v in engine.cfg.campuses.items()},
            "fields": [{"key": k, "label": lab, "required": req} for k, lab, req in FIELDS]}


# ---------------- polling ----------------
def _note_status(engine, sid: str, now: datetime, **info) -> None:
    try:
        engine.store.kv_set("sheet_status_" + sid, json.dumps({"at": now.astimezone(engine.cfg.tz).strftime("%d %b %H:%M"), **info}))
    except Exception:
        pass


def poll_all(engine, now: datetime, budget: float = 35.0) -> dict:
    import time
    t0 = time.monotonic()
    from .sources.csv_source import lead_from_row
    creds = google_json(engine)
    sheets = configured(engine)
    if not (creds and sheets):
        return {"skipped": "no sheet or key configured"}
    out = {}
    for s in sheets:
        key = "sheet_baselined_" + s["id"]
        if time.monotonic() - t0 > budget:               # never hold up the calls: the rest is read on the next minute
            out[s["label"]] = {"deferred": "time budget used; read again next minute"}
            continue
        try:
            rows = read_rows(creds, s["spreadsheet_id"], s["worksheet"])
        except Exception as e:
            out[s["label"]] = {"error": _friendly(e, creds)}
            _note_status(engine, s["id"], now, error=_friendly(e, creds))
            continue
        leads = []
        yflags: dict = {}                                 # parents applying for an earlier academic year: never called
        yc, ay = str(s.get("year_column") or ""), str(s.get("allowed_year") or "")
        flags: dict = {}                                  # parents the school already worked on (their remarks say so): never called
        sk_cols = s.get("skip_columns") or []
        sk_words = [w.lower() for w in (s.get("skip_words") or DEFAULT_SKIP_WORDS)]
        for i, row in enumerate(rows, start=2):
            try:
                lead = lead_from_row(row, s["columns"], engine.cfg.tz, "sheet_" + s["id"], f"row {i}",
                                     engine.cfg.sources.get("date_order", "mdy"))
            except Exception:
                continue
            leads.append(lead)
            if yc and ay:
                yw = _year_reason(row, yc, ay)
                if yw:
                    yflags[lead.key] = yw
            if sk_cols:
                why = _skip_reason(row, sk_cols, sk_words)
                if why:
                    flags[lead.key] = why
        first = not engine.store.kv_get(key)
        sf = str(s.get("start_from") or "").strip()
        bf, bt = str(s.get("backfill_from") or ""), str(s.get("backfill_to") or "")
        import hashlib                                   # nothing new in the sheet and nothing changed in its settings: skip the database work
        fp = hashlib.sha1(json.dumps([len(leads), [x.key for x in leads[-3:]], sf, bf, bt, s["columns"], sorted(flags.items()), sk_cols, sk_words, sorted(yflags.items()), yc, ay], sort_keys=True,
                                     default=str).encode()).hexdigest()
        if (not first and leads and engine.store.kv_get("sheet_fp_" + s["id"]) == fp
                and (not (bf and bt) or engine.store.kv_get("sheet_backfill_" + s["id"]) == f"{bf}|{bt}")):
            out[s["label"]] = {"new": 0, "unchanged": True}
            _note_status(engine, s["id"], now, rows=len(rows), readable=len(leads), new=0, error="")
            continue
        since = None
        if sf:
            since = datetime.strptime(sf, "%Y-%m-%d").replace(tzinfo=engine.cfg.tz)
            seen = engine.store.kv_get("sheet_since_" + s["id"])
            if seen is not None and seen != sf and (engine.store.kv_get(key) or ""):     # date changed: re-open what the new date allows
                engine.revive_since("sheet_" + s["id"], since, now)
            engine.store.kv_set("sheet_since_" + s["id"], sf)
        res = engine.ingest(leads, now, baseline=first and since is None, since=since)
        if flags:
            res["skipped_by_remarks"] = engine.apply_skips("sheet_" + s["id"], flags, now)
        if yflags:
            res["skipped_by_year"] = engine.apply_skips("sheet_" + s["id"], yflags, now, "applying for an earlier year")
        if since:
            res["from_date"] = sf
        if bf and bt and engine.store.kv_get("sheet_backfill_" + s["id"]) != f"{bf}|{bt}":
            res["backfill"] = engine.revive_range("sheet_" + s["id"], datetime.strptime(bf, "%Y-%m-%d").replace(tzinfo=engine.cfg.tz),
                                                  datetime.strptime(bt, "%Y-%m-%d").replace(tzinfo=engine.cfg.tz), now,
                                                  deadline=t0 + budget)
            if not res["backfill"].get("partial"):          # a big range is finished over several minutes, never in one long request
                engine.store.kv_set("sheet_backfill_" + s["id"], f"{bf}|{bt}")
        if first:
            engine.store.kv_set(key, iso(now))
            res["baseline"] = since is None
        if not (res.get("backfill") or {}).get("partial"):
            engine.store.kv_set("sheet_fp_" + s["id"], fp)
        out[s["label"]] = res
        _note_status(engine, s["id"], now, rows=len(rows), readable=len(leads), new=res.get("new", 0),
                     error="" if leads else "no row could be read: check the columns chosen for this sheet")
    return out
