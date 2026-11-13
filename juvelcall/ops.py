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
    engine._base = {"calling": copy.deepcopy(cfg.calling), "campuses": copy.deepcopy(cfg.campuses)}


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
    cfg.calling.clear(); cfg.calling.update(calling)
    cfg.campuses.clear(); cfg.campuses.update(campuses)
    engine.c = cfg.calling


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
