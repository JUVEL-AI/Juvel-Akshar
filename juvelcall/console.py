"""Read-only data for the dashboard (Next.js on Vercel calls this server-side)."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta

from .store import parse


def mask(phone: str | None) -> str:
    if not phone or len(phone) < 8:
        return phone or ""
    return f"{phone[:3]} {phone[3:5]}••• ••{phone[-3:]}"


def build(engine, now: datetime, day: str | None = None) -> dict:
    cfg, store = engine.cfg, engine.store
    local_now = now.astimezone(cfg.tz)
    day_start = (datetime.fromisoformat(day).replace(tzinfo=cfg.tz) if day
                 else local_now.replace(hour=0, minute=0, second=0, microsecond=0))
    day_end = day_start + timedelta(days=1)
    from .store import iso
    rows = store.q("SELECT * FROM leads WHERE created_at>=? AND created_at<? ORDER BY created_at DESC",
                   (iso(day_start), iso(day_end)))
    calls = {}
    for c in store.q("SELECT lead_id, MIN(started_at) first_at, MAX(attempt) n FROM calls GROUP BY lead_id"):
        calls[c["lead_id"]] = c
    leads = []
    for r in rows:
        created = parse(r["created_at"])
        c = calls.get(r["id"])
        first = None
        if c and c["first_at"]:
            first = max(0, int((parse(c["first_at"]) - created).total_seconds()))
        leads.append({
            "id": r["id"], "time": created.astimezone(cfg.tz).strftime("%H:%M"),
            "parent": r["name"] or "", "student": r["student_name"] or "", "class": r["grade"] or "",
            "campus": r["campus"] or "", "phone": mask(r["phone"]), "status": r["status"],
            "attempts": r["attempts"], "first_call_seconds": first, "note": r["note"] or "",
            "transferred_to": r["transferred_to"] or ""})
    cnt = Counter(l["status"] for l in leads)
    called = [l for l in leads if l["first_call_seconds"] is not None]
    fast = sum(1 for l in called if l["first_call_seconds"] <= 300)
    secs = sorted(l["first_call_seconds"] for l in called)
    hours = Counter(int(l["time"][:2]) for l in leads)
    attention = [l for l in leads if l["status"] in
                 ("ANSWERED_NOT_TRANSFERRED", "NEEDS_REVIEW", "NO_RESPONSE", "INVALID_PHONE", "STALE")]
    return {
        "generated_at": now.isoformat(), "day": day_start.date().isoformat(),
        "campuses": {k: v["label"] for k, v in cfg.campuses.items()},
        "kpis": {"enquiries": len(leads), "called": len(called),
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
