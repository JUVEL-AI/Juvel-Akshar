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
    ("grade", "Class / grade", False), ("student_name", "Student name", False), ("created_at", "Time of enquiry", True),
    ("email", "Email", False), ("city", "City", False)]
REQUIRED = [k for k, _, r in FIELDS if r]
ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


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
        "phone": pick(lambda n: any(w in n for w in ("phone", "mobile", "whatsapp", "contact number", "contact no"))),
        "campus": pick(lambda n: "campus" in n or "branch" in n),
        "grade": pick(lambda n: any(w in n for w in ("grade", "class", "standard", "std")), avoid=("school", "campaign")),
        "email": pick(lambda n: "email" in n or "e mail" in n),
        "city": pick(lambda n: n in ("city", "location", "area") or n.endswith(" city")),
        "created_at": pick(lambda n: any(w in n for w in ("timing", "timestamp", "created", "submitted", "date", "time")) and "willing" not in n),
        "student_name": pick(lambda n: any(w in n for w in ("student", "child")) and "name" in n, avoid=("school", "grade", "class")),
    }
    out["name"] = pick(lambda n: "name" in n, avoid=("student", "child", "ad ", "campaign", "school", "sheet", "profile")) \
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
        out["sheets"].append({"id": str(s.get("id") or sid[:8] + str(i)), "label": label, "spreadsheet_id": sid,
                              "worksheet": ws, "columns": cols, "enabled": bool(s.get("enabled", True))})
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
    return {"sheets": d["sheets"], "service_email": service_email(creds), "has_credentials": bool(creds),
            "default_campus": sc.get("default_campus", "") or "", "date_order": sc.get("date_order", "mdy"),
            "from_server": bool(not d["sheets"] and configured(engine)),
            "campuses": {k: v["label"] for k, v in engine.cfg.campuses.items()},
            "fields": [{"key": k, "label": lab, "required": req} for k, lab, req in FIELDS]}


# ---------------- polling ----------------
def poll_all(engine, now: datetime) -> dict:
    from .sources.csv_source import lead_from_row
    creds = google_json(engine)
    sheets = configured(engine)
    if not (creds and sheets):
        return {"skipped": "no sheet or key configured"}
    out = {}
    for s in sheets:
        key = "sheet_baselined_" + s["id"]
        try:
            rows = read_rows(creds, s["spreadsheet_id"], s["worksheet"])
        except Exception as e:
            out[s["label"]] = {"error": _friendly(e, creds)}
            continue
        leads = []
        for i, row in enumerate(rows, start=2):
            try:
                leads.append(lead_from_row(row, s["columns"], engine.cfg.tz, "sheet_" + s["id"], f"row {i}",
                                           engine.cfg.sources.get("date_order", "mdy")))
            except Exception:
                continue
        first = not engine.store.kv_get(key)
        res = engine.ingest(leads, now, baseline=first)
        if first:
            engine.store.kv_set(key, iso(now))
            res["baseline"] = True
        out[s["label"]] = res
    return out
