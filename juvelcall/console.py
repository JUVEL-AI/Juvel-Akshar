"""Read-only data for the dashboard (Next.js on Vercel calls this server-side)."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta

from . import ops
from .models import Status
from .store import parse


def mask(phone: str | None) -> str:
    if not phone or len(phone) < 8:
        return phone or ""
    return f"{phone[:3]} {phone[3:5]}••• ••{phone[-3:]}"


def build(engine, now: datetime, day: str | None = None, test: bool = False) -> dict:
    cfg, store = engine.cfg, engine.store
    local_now = now.astimezone(cfg.tz)
    day_start = (datetime.fromisoformat(day).replace(tzinfo=cfg.tz) if day
                 else local_now.replace(hour=0, minute=0, second=0, microsecond=0))
    day_end = day_start + timedelta(days=1)
    from .store import iso
    if test:                          # the Test lab: only test enquiries, newest 30, any day
        rows = store.q("SELECT * FROM leads WHERE source='test' ORDER BY id DESC LIMIT 30")
    else:                             # everything real; test enquiries never appear here
        rows = store.q(                                         # the day's enquiries, plus older enquiries called that day
            "SELECT * FROM leads WHERE source<>'test' AND ((created_at>=? AND created_at<?) OR id IN "
            "(SELECT c.lead_id FROM calls c JOIN leads l ON l.id=c.lead_id WHERE l.allow_old=1 AND c.started_at>=? AND c.started_at<?)) "
            "ORDER BY created_at DESC", (iso(day_start), iso(day_end), iso(day_start), iso(day_end)))
    calls = {}
    for c in store.q("SELECT lead_id, MIN(started_at) first_at, MAX(attempt) n FROM calls GROUP BY lead_id"):
        calls[c["lead_id"]] = c
    recs: dict = {}
    ids = [r["id"] for r in rows]
    if ids:
        for k in store.q("SELECT * FROM calls WHERE lead_id IN (%s) ORDER BY started_at" % ",".join("?" * len(ids)), tuple(ids)):
            st = parse(k["started_at"])
            recs.setdefault(k["lead_id"], []).append({
                "attempt": k["attempt"], "at": st.astimezone(cfg.tz).strftime("%d %b %H:%M") if st else "",
                "outcome": k["outcome"] or "", "seconds": k["duration_seconds"], "reason": k["ended_reason"] or "",
                "recording": k["recording_url"] or "", "summary": k["summary"] or "",
                "transcript": (k["transcript"] or "")[:6000], "detail": k["detail"] or ""})
    leads = []
    for r in rows:
        created = parse(r["created_at"])
        c = calls.get(r["id"])
        first = None
        if c and c["first_at"]:
            first = max(0, int((parse(c["first_at"]) - created).total_seconds()))
        older = bool(not test and r["allow_old"] and not (day_start <= created.astimezone(cfg.tz) < day_end))
        leads.append({
            "id": r["id"], "older": older,
            "time": created.astimezone(cfg.tz).strftime("%d %b" if older else "%H:%M"),
            "parent": r["name"] or "", "student": r["student_name"] or "", "class": r["grade"] or "",
            "campus": r["campus"] or "", "phone": mask(r["phone"]), "status": r["status"],
            "attempts": r["attempts"], "first_call_seconds": first, "note": r["note"] or "",
            "transferred_to": r["transferred_to"] or "", "calls": recs.get(r["id"], [])})
    cnt = Counter(l["status"] for l in leads)
    fresh = [l for l in leads if not l["older"]]       # older enquiries (catch-up) are listed but kept out of the speed figures
    called = [l for l in fresh if l["first_call_seconds"] is not None]
    fast = sum(1 for l in called if l["first_call_seconds"] <= 300)
    secs = sorted(l["first_call_seconds"] for l in called)
    hours = Counter(int(l["time"][:2]) for l in fresh)
    attention = [l for l in leads if l["status"] in
                 ("ANSWERED_NOT_TRANSFERRED", "NEEDS_REVIEW", "NO_RESPONSE", "INVALID_PHONE", "STALE")]
    live = []
    if not test:                      # calls on the line right now: who the counselor is about to speak to
        for r in store.q("SELECT * FROM leads WHERE source<>'test' AND status=? AND updated_at>=? ORDER BY updated_at DESC",
                         (Status.CALLING.value, iso(now - timedelta(minutes=10)))):
            live.append({"id": r["id"], "parent": r["name"] or "", "student": r["student_name"] or "", "class": r["grade"] or "",
                         "campus": r["campus"] or "", "attempt": r["attempts"],
                         "since": parse(r["updated_at"]).astimezone(cfg.tz).strftime("%H:%M")})
    return {
        "live": live,
        "generated_at": now.isoformat(), "day": day_start.date().isoformat(), "control": engine.control_info(),
        "records_staff": bool(ops._stored(engine).get("records_staff", False)),
        "hours_label": ops.hours_label(cfg.calling),
        "campus_status": {k: {"label": v["label"], "on_duty": (engine._on_duty(k) or {}).get("name"),
                              "counselors": [{"name": c["name"], "number": (c["numbers"] or [""])[0],
                                              "on_duty": c.get("on_duty", True)} for c in v["counselors"]]}
                          for k, v in cfg.campuses.items()},
        "campuses": {k: v["label"] for k, v in cfg.campuses.items()},
        "kpis": {"enquiries": len(fresh), "older_called": len(leads) - len(fresh), "called": len(called),
                 "called_within_5_min_pct": round(fast * 100 / len(called)) if called else 0,
                 "median_first_call_seconds": secs[len(secs) // 2] if secs else 0,
                 "transferred": cnt["TRANSFERRED"], "callbacks_needed": cnt["ANSWERED_NOT_TRANSFERRED"]},
        "outcomes": dict(cnt), "by_hour": dict(sorted(hours.items())),
        "attention": attention, "leads": leads}


def lead_detail(engine, lead_id: int) -> dict | None:
    s = engine.store
    r = s.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not r:
        return None
    return {"lead": {k: r[k] for k in ("id", "name", "student_name", "grade", "campus", "status", "attempts", "note", "transferred_to", "created_at")}
            | {"phone": mask(r["phone"])},
            "calls": [dict(c) for c in s.q("SELECT call_ref,attempt,started_at,outcome,detail,ended_at FROM calls WHERE lead_id=? ORDER BY started_at", (lead_id,))],
            "alerts": [dict(a) for a in s.q("SELECT created_at,kind,subject FROM alerts WHERE lead_id=? ORDER BY id", (lead_id,))]}
