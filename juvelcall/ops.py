"""Everything around the call engine: settings edited from the dashboard, daily/weekly reports,
call-back reminders, CSV export and search. Kept apart so the call path stays small."""
from __future__ import annotations
import copy
import csv
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .normalize import clean_phone
from .rules import in_hours, window
from .store import iso, parse
from .models import Status

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAME = dict(zip(DAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------------- settings ----------------
def snapshot_base(engine) -> None:
    """Remember the config.yaml values so dashboard overrides can be layered on and removed."""
    cfg = engine.cfg
    engine._base = {"calling": copy.deepcopy(cfg.calling), "campuses": copy.deepcopy(cfg.campuses),
                    "dialer": copy.deepcopy(cfg.dialer),
                    "sources": {"default_campus": cfg.sources.get("default_campus", ""), "date_order": cfg.sources.get("date_order", "mdy")}}


def _stored(engine) -> dict:
    try:
        return json.loads(engine.store.kv_get("settings") or "{}")
    except ValueError:
        return {}


def apply_overrides(engine) -> None:
    """Called at the start of every tick / ingest, so a warm serverless instance never uses stale settings."""
    if not hasattr(engine, "_base"):
        snapshot_base(engine)
    cfg, base, st = engine.cfg, engine._base, _stored(engine)
    calling = copy.deepcopy(base["calling"])
    campuses = copy.deepcopy(base["campuses"])
    if "hours" in st:
        calling["hours"] = {d: v for d, v in st["hours"].items() if v}
    if "retry_minutes" in st:
        calling["retry_minutes"] = st["retry_minutes"]
    if "max_attempts" in st:
        calling["max_attempts"] = st["max_attempts"]
    for key, ov in (st.get("campuses") or {}).items():
        if key not in campuses:
            continue
        if "alert_emails" in ov:
            campuses[key]["alert_emails"] = ov["alert_emails"]
        if "counselors" in ov:
            campuses[key]["counselors"] = [
                {"name": c["name"], "numbers": [c["number"]], "on_duty": bool(c.get("on_duty", True))}
                for c in ov["counselors"]]
    integ = _integrations(engine)
    dialer = copy.deepcopy(base["dialer"])
    if integ.get("vapi_voice_id"):
        v = dict(dialer.get("vapi") or {}); v["voice_id"] = integ["vapi_voice_id"]; dialer["vapi"] = v
    if integ.get("vapi_transfer_style") in ("blind", "assistant"):
        v = dict(dialer.get("vapi") or {}); v["transfer_style"] = integ["vapi_transfer_style"]; dialer["vapi"] = v
    if integ.get("caller_id"):
        for c in campuses.values():
            c["caller_id"] = integ["caller_id"]
    if integ.get("vapi_api_key") or integ.get("vapi_phone_number_id") or integ.get("vapi_server_secret"):
        v = dict(dialer.get("vapi") or {})
        for k_in, k_out in (("vapi_api_key", "api_key"), ("vapi_phone_number_id", "phone_number_id"),
                            ("vapi_server_secret", "server_secret")):
            if integ.get(k_in):
                v[k_out] = integ[k_in]
        dialer["vapi"] = v
        if v.get("api_key") and v.get("phone_number_id"):
            dialer["type"] = "vapi"
    try:
        sm = json.loads(engine.store.kv_get("sheets") or "{}")
    except ValueError:
        sm = {}
    cfg.sources["default_campus"] = sm["default_campus"] if "default_campus" in sm else base["sources"]["default_campus"]
    cfg.sources["date_order"] = sm.get("date_order") or base["sources"]["date_order"]
    cfg.calling.clear(); cfg.calling.update(calling)
    cfg.campuses.clear(); cfg.campuses.update(campuses)
    changed = dialer != cfg.dialer
    cfg.dialer.clear(); cfg.dialer.update(dialer)
    engine.c = cfg.calling
    if changed:
        from .dialers import dialer_from_config
        engine.dialer = dialer_from_config(cfg)


# ---------------- integrations (Vapi / Vobiz), admin only ----------------
SECRET_KEYS = ("vapi_api_key", "vapi_server_secret", "google_service_account")
INTEG_KEYS = ("vapi_api_key", "vapi_phone_number_id", "vapi_server_secret", "caller_id", "vapi_voice_id", "vapi_transfer_style", "google_service_account")


def _integrations(engine) -> dict:
    try:
        return json.loads(engine.store.kv_get("integrations") or "{}")
    except ValueError:
        return {}


def _mask(v: str) -> str:
    return ("•" * 8 + v[-4:]) if len(v) > 4 else ("•" * len(v))


def integrations_view(engine) -> dict:
    """What the admin page shows. Secrets are never sent back in full, only the last 4 characters."""
    cfg, st = engine.cfg, _integrations(engine)
    v = cfg.dialer.get("vapi") or {}
    caller = next((c.get("caller_id", "") for c in cfg.campuses.values()), "")
    eff = {"vapi_api_key": v.get("api_key", ""), "vapi_phone_number_id": v.get("phone_number_id", ""),
           "vapi_server_secret": v.get("server_secret", ""), "caller_id": caller,
           "vapi_voice_id": v.get("voice_id", ""), "vapi_transfer_style": v.get("transfer_style", "blind")}
    out = {}
    for k in INTEG_KEYS:
        val = eff.get(k) or ""
        out[k] = {"set": bool(val), "value": _mask(val) if (k in SECRET_KEYS and val) else val,
                  "from": "dashboard" if st.get(k) else ("server settings" if val else "")}
    from .sheets_admin import google_json, service_email
    g = google_json(engine)
    out["google_service_account"] = {"set": bool(g), "value": service_email(g) or ("•" * 8 if g else ""),
                                     "from": "dashboard" if st.get("google_service_account") else ("server settings" if g else "")}
    out["live"] = bool(eff["vapi_api_key"] and eff["vapi_phone_number_id"] and cfg.dialer.get("type") == "vapi")
    return out


def save_integrations(engine, payload: dict, now: datetime, who: str) -> dict:
    """Blank field = keep what is saved. 'clear:<field>' in payload['clear'] removes a saved value."""
    st = _integrations(engine)
    changed = []
    for k in INTEG_KEYS:
        val = str(payload.get(k, "") or "").strip()
        if not val:
            continue
        if k == "caller_id":
            n = clean_phone(val)
            if not n:
                raise ValueError("Calling number: that is not a valid phone number")
            counselors = {clean_phone(x) for c in engine.cfg.campuses.values()
                          for co in c.get("counselors", []) for x in co["numbers"]}
            if n in counselors:
                raise ValueError("The calling number cannot be the same as a counselor's number")
            val = n
        elif k == "google_service_account":
            from .sheets_admin import validate_google_json
            val = validate_google_json(val)
        elif k == "vapi_transfer_style":
            if val not in ("blind", "assistant"):
                raise ValueError("Transfer style must be 'blind' or 'assistant'")
        elif k != "google_service_account" and any(ch.isspace() for ch in val):
            raise ValueError(f"{k.replace('_', ' ')}: remove the spaces")
        if k.startswith("vapi_") and k not in ("vapi_voice_id", "vapi_transfer_style") and len(val) < 8:
            raise ValueError(f"{k.replace('_', ' ')} looks too short")
        st[k] = val
        changed.append(k)
    for k in payload.get("clear") or []:
        if k in INTEG_KEYS and k in st:
            del st[k]
            changed.append(k)
    engine.store.kv_set("integrations", json.dumps(st))
    engine.store.add_audit(now, who, "integrations", "changed: " + (", ".join(changed) or "nothing"))
    apply_overrides(engine)
    return integrations_view(engine)


def test_vapi(engine) -> dict:
    """Asks Vapi whether the saved key can see the saved phone number. Places no call."""
    import requests
    v = engine.cfg.dialer.get("vapi") or {}
    if not (v.get("api_key") and v.get("phone_number_id")):
        return {"ok": False, "message": "Save the Vapi key and the phone number ID first."}
    try:
        r = requests.get(f"https://api.vapi.ai/phone-number/{v['phone_number_id']}",
                         headers={"Authorization": f"Bearer {v['api_key']}"}, timeout=10)
    except Exception as e:
        return {"ok": False, "message": f"Could not reach Vapi: {e}"}
    if r.status_code == 401:
        return {"ok": False, "message": "Vapi rejected the API key. Use the private key from Vapi > API Keys."}
    if r.status_code == 404:
        return {"ok": False, "message": "Vapi does not know that phone number ID under this key."}
    if r.status_code >= 400:
        return {"ok": False, "message": f"Vapi answered {r.status_code}."}
    j = r.json() if r.content else {}
    return {"ok": True, "message": "Vapi accepted the key and found the number " + str(j.get("number") or j.get("name") or "") + "."}


def hours_map(calling: dict) -> dict:
    out = {}
    for i, d in enumerate(DAYS):
        w = window(calling, i)
        out[d] = [w[0].strftime("%H:%M"), w[1].strftime("%H:%M")] if w else None
    return out


def hours_label(calling: dict) -> str:
    """'Mon-Fri 8:30-2:30 · Sat 9:00-2:00' from the effective hours."""
    def f(hm):
        h, m = int(hm[:2]), hm[3:]
        return f"{h % 12 or 12}:{m}"
    groups, cur = [], None
    for d, v in hours_map(calling).items():
        if not v:
            cur = None
            continue
        if cur and cur[2] == v:
            cur[1] = d
        else:
            cur = [d, d, v]
            groups.append(cur)
    return " · ".join(
        (DAY_NAME[a] if a == b else f"{DAY_NAME[a]}–{DAY_NAME[b]}") + f" {f(v[0])}–{f(v[1])}" for a, b, v in groups) or "No calling days"


def effective_settings(engine) -> dict:
    cfg = engine.cfg
    return {
        "hours": hours_map(cfg.calling),
        "retry_minutes": list(cfg.calling["retry_minutes"]),
        "max_attempts": cfg.calling["max_attempts"],
        "campuses": {k: {"label": v["label"], "alert_emails": list(v.get("alert_emails", [])),
                         "counselors": [{"name": c["name"], "number": (c["numbers"] or [""])[0],
                                         "on_duty": c.get("on_duty", True)} for c in v["counselors"]]}
                     for k, v in cfg.campuses.items()},
        "admin_emails": list(cfg.notifications.get("admin_emails", [])),
        "records_staff": bool(_stored(engine).get("records_staff", False)),
    }


def validate_settings(engine, payload: dict) -> dict:
    """Returns the cleaned override dict, or raises ValueError with a message the admin can read."""
    out: dict = {}
    if "hours" in payload:
        hrs = {}
        for d in DAYS:
            v = (payload["hours"] or {}).get(d)
            if not v:
                hrs[d] = None
                continue
            if len(v) != 2 or not HM.match(str(v[0])) or not HM.match(str(v[1])):
                raise ValueError(f"{DAY_NAME[d]}: times must look like 08:30")
            if v[0] >= v[1]:
                raise ValueError(f"{DAY_NAME[d]}: closing time must be after opening time")
            hrs[d] = [v[0], v[1]]
        if not any(hrs.values()):
            raise ValueError("At least one day must have calling hours")
        out["hours"] = hrs
    if "max_attempts" in payload:
        n = payload["max_attempts"]
        if not isinstance(n, int) or not 1 <= n <= 6:
            raise ValueError("Attempts per parent must be a number from 1 to 6")
        out["max_attempts"] = n
    if "retry_minutes" in payload:
        r = payload["retry_minutes"]
        if (not isinstance(r, list) or not r or len(r) > 5
                or not all(isinstance(x, int) and 1 <= x <= 1440 for x in r)):
            raise ValueError("Retry gaps must be 1 to 5 whole numbers of minutes (1 to 1440)")
        out["retry_minutes"] = r
    if "records_staff" in payload:
        out["records_staff"] = bool(payload["records_staff"])
    if "campuses" in payload:
        camps = {}
        for key, ov in (payload["campuses"] or {}).items():
            if key not in engine._base["campuses"]:
                raise ValueError(f"Unknown campus '{key}'")
            c = {}
            if "alert_emails" in ov:
                mails = [m.strip() for m in ov["alert_emails"] if str(m).strip()]
                if not mails or not all(EMAIL.match(m) for m in mails):
                    raise ValueError("Alert emails must be valid addresses (at least one)")
                c["alert_emails"] = mails
            if "counselors" in ov:
                cs = []
                for x in ov["counselors"]:
                    name = str(x.get("name", "")).strip()
                    num = clean_phone(str(x.get("number", "")))
                    if not name or len(name) > 40 or "{" in name or "}" in name:
                        raise ValueError("Each counselor needs a name (no curly brackets)")
                    if not num:
                        raise ValueError(f"{name}: that phone number is not valid")
                    if num in {clean_phone(v["caller_id"]) for v in engine.cfg.campuses.values() if v.get("caller_id")}:
                        raise ValueError(f"{name}: that is the number the system calls from. "
                                         "A counselor number must be a different phone, or calls would loop")
                    cs.append({"name": name, "number": num, "on_duty": bool(x.get("on_duty", True))})
                if not cs:
                    raise ValueError("Each campus needs at least one counselor")
                c["counselors"] = cs
            camps[key] = c
        out["campuses"] = camps
    return out


def save_settings(engine, payload: dict, now: datetime, who: str) -> dict:
    clean = validate_settings(engine, payload)
    st = _stored(engine)
    for k, v in clean.items():
        if k == "campuses":
            st.setdefault("campuses", {})
            for ck, cv in v.items():
                st["campuses"].setdefault(ck, {}).update(cv)
        else:
            st[k] = v
    engine.store.kv_set("settings", json.dumps(st))
    engine.store.add_audit(now, who, "settings", ", ".join(sorted(clean)))
    apply_overrides(engine)
    return effective_settings(engine)


# ---------------- reports ----------------
def _day_rows(engine, day_start: datetime):
    return engine.store.q("SELECT * FROM leads WHERE created_at>=? AND created_at<? ORDER BY created_at",
                          (iso(day_start), iso(day_start + timedelta(days=1))))


def _first_call_seconds(engine) -> dict:
    out = {}
    for c in engine.store.q("SELECT l.id id, l.created_at made, MIN(c.started_at) first FROM leads l "
                            "JOIN calls c ON c.lead_id=l.id GROUP BY l.id, l.created_at"):
        out[c["id"]] = max(0, int((parse(c["first"]) - parse(c["made"])).total_seconds()))
    return out


def _stats(rows, first) -> dict:
    n = len(rows)
    cnt = Counter(r["status"] for r in rows)
    secs = sorted(first[r["id"]] for r in rows if r["id"] in first)
    fast = sum(1 for s in secs if s <= 300)
    return {"enquiries": n, "transferred": cnt["TRANSFERRED"], "callbacks": cnt["ANSWERED_NOT_TRANSFERRED"],
            "no_response": cnt["NO_RESPONSE"], "review": cnt["NEEDS_REVIEW"] + cnt["INVALID_PHONE"],
            "called": len(secs), "fast_pct": round(fast * 100 / len(secs)) if secs else 0,
            "median": secs[len(secs) // 2] if secs else 0}


def _mm(s: int) -> str:
    return f"{s // 60}m {s % 60:02d}s"


def daily_summary(engine, now: datetime):
    """[(recipients, subject, body)] for today, one per campus plus one combined for admins."""
    cfg = engine.cfg
    local = now.astimezone(cfg.tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    rows, first = _day_rows(engine, start), _first_call_seconds(engine)
    mails, campus_inboxes = [], set()
    label = f"{local:%a %d %b %Y}"
    for key, camp in cfg.campuses.items():
        mine = [r for r in rows if r["campus"] == key]
        st = _stats(mine, first)
        todo = [r for r in mine if r["status"] in ("ANSWERED_NOT_TRANSFERRED", "NO_RESPONSE", "NEEDS_REVIEW", "INVALID_PHONE")]
        body = (f"{camp['label']} campus: admissions calling summary for {label}\n\n"
                f"Enquiries received: {st['enquiries']}\n"
                f"Connected to a counselor: {st['transferred']}\n"
                f"Called within 5 minutes: {st['fast_pct']}% (median first call {_mm(st['median'])})\n"
                f"Call-backs still needed: {st['callbacks']}\n"
                f"Parents not reachable: {st['no_response']}\n"
                f"Needs a person to check: {st['review']}\n")
        if todo:
            body += "\nPlease follow up by hand:\n" + "\n".join(
                f"- {r['name'] or 'Parent'} / {r['phone'] or r['phone_raw']} / student {r['student_name'] or '-'} / {r['status']}"
                for r in todo)
        to = list(camp.get("alert_emails", []))
        campus_inboxes.update(to)
        mails.append((to, f"Admissions calling summary, {camp['label']}, {label}", body))
    total = _stats(rows, first)
    admin = [a for a in cfg.notifications.get("admin_emails", []) if a not in campus_inboxes]
    if admin:
        mails.append((admin, f"Admissions calling summary, all campuses, {label}",
                      f"All campuses, {label}\n\nEnquiries: {total['enquiries']}\nConnected: {total['transferred']}\n"
                      f"Called within 5 minutes: {total['fast_pct']}%\nCall-backs needed: {total['callbacks']}\n"
                      f"Not reachable: {total['no_response']}\nNeeds checking: {total['review']}\n"))
    return mails


def weekly_report(engine, now: datetime):
    cfg = engine.cfg
    local = now.astimezone(cfg.tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = engine.store.q("SELECT * FROM leads WHERE created_at>=? AND created_at<? ORDER BY created_at",
                          (iso(monday), iso(monday + timedelta(days=7))))
    first = _first_call_seconds(engine)
    lines = [f"Weekly admissions calling report: {monday:%d %b} to {(monday + timedelta(days=5)):%d %b %Y}", ""]
    t = _stats(rows, first)
    lines += [f"All campuses: {t['enquiries']} enquiries, {t['transferred']} connected, "
              f"{t['fast_pct']}% called within 5 minutes (median {_mm(t['median'])}), "
              f"{t['no_response']} not reachable.", ""]
    for key, camp in cfg.campuses.items():
        s = _stats([r for r in rows if r["campus"] == key], first)
        lines.append(f"{camp['label']}: {s['enquiries']} enquiries, {s['transferred']} connected, "
                     f"{s['callbacks']} call-backs, {s['no_response']} not reachable")
    src = Counter(r["source"] for r in rows)
    lines += ["", "Where enquiries came from: " + (", ".join(f"{k} {v}" for k, v in src.most_common()) or "none")]
    by_hour = defaultdict(lambda: [0, 0])
    for r in rows:
        h = parse(r["created_at"]).astimezone(cfg.tz).hour
        by_hour[h][0] += 1
        by_hour[h][1] += r["status"] == "TRANSFERRED"
    best = sorted(((h, v) for h, v in by_hour.items() if v[0] >= 3), key=lambda x: -x[1][1] / x[1][0])[:3]
    if best:
        lines.append("Hours with the best connect rate (enquiries arriving in that hour): " +
                     ", ".join(f"{h}:00 ({round(v[1] * 100 / v[0])}% of {v[0]})" for h, v in best))
    lines.append(f"Enquiries that arrived outside calling hours: "
                 f"{sum(1 for r in rows if not in_hours(parse(r['created_at']), cfg.calling, cfg.tz))}")
    return (list(cfg.notifications.get("admin_emails", [])),
            f"Weekly admissions calling report, week of {monday:%d %b %Y}", "\n".join(lines))


def periodic(engine, now: datetime) -> dict:
    """Run from /admin/tick each minute. Each job runs at most once per day/week (kv flags)."""
    out: dict = {}
    cfg, store = engine.cfg, engine.store
    apply_overrides(engine)
    # call-back reminders (only inside calling hours, only for recent leads)
    mins = cfg.calling.get("callback_reminder_minutes", [60, 240])
    if in_hours(now, cfg.calling, cfg.tz) and mins:
        sent = 0
        recent = iso(now - timedelta(days=3))
        for r in store.q("SELECT * FROM leads WHERE status IN (?,?) AND updated_at>=?",
                         (Status.ANSWERED_NOT_TRANSFERRED.value, Status.NO_RESPONSE.value, recent)):
            waited = (now - parse(r["updated_at"])).total_seconds() / 60
            for i, m in enumerate(mins):
                kind = f"callback_reminder_{i + 1}"
                if waited >= m and not store.one("SELECT 1 FROM alerts WHERE lead_id=? AND kind=?", (r["id"], kind)):
                    engine._alert(now, kind, r["id"], f"REMINDER: parent still waiting for a call back ({int(waited)} min)",
                                  engine._lead_summary(r) + "\nThis parent has not been marked as called back yet. "
                                  "After you call, press 'Mark done' on the dashboard.")
                    sent += 1
                    break
        out["reminders"] = sent
    local = now.astimezone(cfg.tz)
    w = window(cfg.calling, local.weekday())
    delay = timedelta(minutes=cfg.notifications.get("report_minutes_after_close", 30))
    if w:
        close = datetime.combine(local.date(), w[1], tzinfo=cfg.tz) + delay
        key = f"summary_{local.date()}"
        if now >= close and not store.kv_get(key):
            store.kv_set(key, "1")
            for to, subj, body in daily_summary(engine, now):
                if to:
                    engine.notifier.send(to, subj, body)
            out["daily_summary"] = True
        last_day = max((i for i in range(7) if window(cfg.calling, i)), default=None)
        wkey = f"weekly_{local.isocalendar()[0]}-W{local.isocalendar()[1]}"
        if local.weekday() == last_day and now >= close and not store.kv_get(wkey):
            store.kv_set(wkey, "1")
            to, subj, body = weekly_report(engine, now)
            if to:
                engine.notifier.send(to, subj, body)
            out["weekly_report"] = True
    return out


# ---------------- export / search ----------------
def export_csv(engine, day: str | None, now: datetime) -> str:
    cfg = engine.cfg
    local = now.astimezone(cfg.tz)
    start = (datetime.fromisoformat(day).replace(tzinfo=cfg.tz) if day
             else local.replace(hour=0, minute=0, second=0, microsecond=0))
    rows, first = _day_rows(engine, start), _first_call_seconds(engine)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Time", "Parent", "Student", "Class", "Campus", "Phone", "Status", "Attempts",
                "First call after (seconds)", "Connected to", "Note"])
    for r in rows:
        w.writerow([parse(r["created_at"]).astimezone(cfg.tz).strftime("%d-%m-%Y %H:%M"), r["name"] or "",
                    r["student_name"] or "", r["grade"] or "", cfg.campuses.get(r["campus"], {}).get("label", r["campus"] or ""),
                    r["phone"] or r["phone_raw"] or "", r["status"], r["attempts"], first.get(r["id"], ""),
                    r["transferred_to"] or "", r["note"] or ""])
    return buf.getvalue()


def search(engine, q: str, limit: int = 40) -> list[dict]:
    from .console import mask
    q = q.strip()
    if len(q) < 3:
        return []
    digits = re.sub(r"\D", "", q)
    like = f"%{q.lower()}%"
    if len(digits) >= 4:
        rows = engine.store.q("SELECT * FROM leads WHERE phone LIKE ? OR phone_raw LIKE ? ORDER BY id DESC LIMIT ?",
                              (f"%{digits}%", f"%{digits}%", limit))
    else:
        rows = engine.store.q("SELECT * FROM leads WHERE LOWER(name) LIKE ? OR LOWER(student_name) LIKE ? "
                              "ORDER BY id DESC LIMIT ?", (like, like, limit))
    tz = engine.cfg.tz
    return [{"id": r["id"], "when": parse(r["created_at"]).astimezone(tz).strftime("%d %b %H:%M"),
             "parent": r["name"] or "", "student": r["student_name"] or "", "class": r["grade"] or "",
             "campus": engine.cfg.campuses.get(r["campus"], {}).get("label", r["campus"] or ""),
             "phone": mask(r["phone"]), "status": r["status"], "attempts": r["attempts"], "note": r["note"] or ""}
            for r in rows]
