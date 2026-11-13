"""Pre-flight checks and a single real test call."""
from __future__ import annotations
import re
from datetime import datetime, timezone

from .app import build_engine
from .models import RawLead
from .normalize import clean_phone

E164 = re.compile(r"^\+\d{10,14}$")


def run_check(cfg) -> int:
    problems, notes = [], []
    for k, c in cfg.campuses.items():
        nums = [c["caller_id"]] + [n for x in c["counselors"] for n in x["numbers"]] + c.get("backup_numbers", [])
        for n in nums:
            if not E164.match(n) or n.startswith("+9100000"):
                problems.append(f"campus {k}: number {n} is a placeholder / not valid")
    d = cfg.dialer
    t = d.get("type", "mock")
    if t == "mock":
        problems.append("dialer.type is 'mock' - no real calls will be made")
    elif t == "vapi":
        for k in ("api_key", "phone_number_id"):
            if not d["vapi"].get(k):
                problems.append(f"dialer.vapi.{k} is empty")
    if not cfg.data.get("volant_sync", {}).get("url"):
        notes.append("volant_sync.url empty: results will not appear in the Volant dashboard")
    if cfg.security.get("webhook_secret") in ("", "change-me"):
        problems.append("WEBHOOK_SECRET is not set")
    if not (cfg.database or "").startswith("postgres"):
        notes.append("DATABASE_URL not set: using a local SQLite file (fine for tests, lost on Render redeploys)")
    if not cfg.notifications.get("smtp", {}).get("host"):
        notes.append("SMTP not set: alerts are logged only, no email is sent")
    if any("example.com" in e for e in cfg.notifications.get("admin_emails", [])):
        problems.append("admin_emails still example.com")
    for x in notes:
        print("note:", x)
    for x in problems:
        print("FIX :", x)
    print("READY" if not problems else f"{len(problems)} thing(s) to fix before going live")
    return 1 if problems else 0


def test_call(cfg, parent_number: str, campus: str = "ecr") -> None:
    """Place ONE real call through the configured dialer to a test phone you control."""
    eng = build_engine(cfg)
    now = datetime.now(timezone.utc)
    phone = clean_phone(parent_number)
    if not phone:
        print("not a valid phone number"); return
    cfg.calling["days"] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    cfg.calling["start"], cfg.calling["end"] = "00:00", "23:59"      # test ignores hours
    cfg.calling.pop("hours", None)
    eng.ingest([RawLead(key=f"test-{now:%H%M%S}", source="test", row_ref="cli", created_at=now,
                        name="Test Parent", student_name="Test Student", phone=phone,
                        campus=campus, grade_text="LKG")], now)
    refs = eng.tick(now)
    row = eng.store.one("SELECT status, note FROM leads ORDER BY id DESC LIMIT 1")
    print("call placed:" if refs else "NOT placed:", refs or dict(row))
