from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient

from juvelcall.app import create_app
from juvelcall.config import Config
from juvelcall.dialers.mock import MockDialer
from juvelcall.engine import Engine
from juvelcall.models import CallEvent, EventKind, RawLead, Status
from juvelcall.normalize import canonical_campus, clean_grade, clean_phone
from juvelcall.notify import MemoryNotifier
from juvelcall.rules import in_hours, next_open
from juvelcall.store import Store

IST = ZoneInfo("Asia/Kolkata")
cfg = Config.load("config.yaml")
MON_10AM = datetime(2026, 10, 19, 10, 0, tzinfo=IST)   # Monday


def mk(now=MON_10AM, **kw):
    s, d, n = Store(), MockDialer(), MemoryNotifier()
    return Engine(cfg, s, d, n), s, d, n


def lead(key="1", phone="9876543210", campus="ecr_campus", grade="LKG", student="Aarav", at=MON_10AM, **kw):
    return RawLead(key=key, source="t", row_ref="r", created_at=at, name="Asha", student_name=student,
                   phone=phone, campus=campus, grade_text=grade, **kw)


# ---- normalisation
@pytest.mark.parametrize("raw,exp", [
    ("p:+919876543210", "+919876543210"), ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"), ("919876543210", "+919876543210"),
    ("12345", None), ("", None), ("abc", None)])
def test_phone(raw, exp):
    assert clean_phone(raw) == exp


def test_campus():
    for v in ["ecr_campus", "ECR", "East Coast Road"]:
        assert canonical_campus(v) == "ecr"
    for v in ["t_nagar_campus", "t.nagar_campus", "T Nagar"]:
        assert canonical_campus(v) == "t_nagar"
    assert canonical_campus("") is None


def test_grade():
    assert clean_grade("LKG") == "LKG"
    assert clean_grade("Class 1") == "1st grade"
    assert clean_grade("5th") == "5th grade"
    assert clean_grade("Sunshine Matriculation School") == ""   # school name, not a class
    assert clean_grade("") == ""


# ---- hours
def test_hours():
    assert in_hours(MON_10AM, cfg.calling, IST)
    sun = datetime(2026, 10, 18, 11, 0, tzinfo=IST)
    night = datetime(2026, 10, 19, 22, 0, tzinfo=IST)
    assert not in_hours(sun, cfg.calling, IST)
    assert next_open(sun, cfg.calling, IST) == datetime(2026, 10, 19, 8, 30, tzinfo=IST)
    assert next_open(night, cfg.calling, IST) == datetime(2026, 10, 20, 8, 30, tzinfo=IST)
    assert next_open(MON_10AM, cfg.calling, IST) == MON_10AM


# ---- engine flow
def test_happy_path_transfer():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    refs = e.tick(MON_10AM)
    assert len(refs) == 1
    plan = d.calls[0]
    assert plan.parent_number == "+919876543210"
    assert plan.caller_id == cfg.campuses["ecr"]["caller_id"]
    assert "Parent Asha, student Aarav, class LKG, ECR campus" in plan.whisper_text
    assert plan.greeting_text.startswith("Hello, calling from Akshar Arbol International School regarding your registration.")
    assert plan.counselor_numbers == cfg.campuses["ecr"]["counselors"][0]["numbers"]   # no backup configured
    e.handle_event(CallEvent(refs[0], EventKind.TRANSFERRED, transferred_to="+910000000011"), MON_10AM)
    assert e.report() == {"TRANSFERRED": 1}


def test_whisper_omits_unclear_class():
    e, s, d, n = mk()
    e.ingest([lead(grade="Sunshine Matriculation School")], MON_10AM)
    e.tick(MON_10AM)
    assert d.calls[0].whisper_text == "Admissions inquiry. Parent Asha, student Aarav, ECR campus. Connecting now."


def test_night_lead_waits_for_morning():
    e, s, d, n = mk()
    at = datetime(2026, 10, 19, 23, 0, tzinfo=IST)
    e.ingest([lead(at=at)], at)
    assert e.tick(at) == []
    assert len(e.tick(datetime(2026, 10, 20, 9, 0, tzinfo=IST))) == 1


def test_retry_then_give_up_with_alert():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    now = MON_10AM
    for attempt in range(cfg.calling["max_attempts"]):
        refs = e.tick(now)
        assert len(refs) == 1, f"attempt {attempt}"
        e.handle_event(CallEvent(refs[0], EventKind.NO_ANSWER), now)
        row = s.one("SELECT * FROM leads")
        if row["next_attempt_at"]:
            now = datetime.fromisoformat(row["next_attempt_at"])
    assert s.one("SELECT status FROM leads")["status"] == Status.NO_RESPONSE.value
    assert any("not reachable" in x[1] for x in n.sent)


def test_transfer_failed_alerts_admin():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    ref = e.tick(MON_10AM)[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), MON_10AM)
    assert s.one("SELECT status FROM leads")["status"] == Status.ANSWERED_NOT_TRANSFERRED.value
    assert any("CALL BACK NEEDED" in x[1] for x in n.sent)


def test_duplicate_phone_and_dedupe_window():
    e, s, d, n = mk()
    e.ingest([lead(key="1"), lead(key="2", at=MON_10AM + timedelta(hours=1))], MON_10AM + timedelta(hours=1))
    assert e.report().get("DUPLICATE") == 1
    # same lead re-read from the sheet is not re-created
    assert e.ingest([lead(key="1")], MON_10AM)["skipped_existing"] == 1


def test_bad_inputs_flagged_not_called():
    e, s, d, n = mk()
    e.ingest([lead(key="a", phone="12345"), lead(key="b", phone="9811111111", campus="Somewhere")], MON_10AM)
    assert e.tick(MON_10AM) == []
    assert e.report() == {"INVALID_PHONE": 1, "NEEDS_REVIEW": 1}
    assert len(n.sent) == 2


def test_stale_lead_not_auto_called():
    e, s, d, n = mk()
    old = MON_10AM - timedelta(days=5)
    e.ingest([lead(at=old)], MON_10AM)
    assert e.report() == {"STALE": 1}


def test_dnc():
    e, s, d, n = mk()
    s.add_dnc("+919876543210", MON_10AM)
    e.ingest([lead()], MON_10AM)
    assert e.report() == {"DO_NOT_CALL": 1}


def test_provider_failure_retries():
    e, s, d, n = mk()
    d.fail_numbers.add("+919876543210")
    e.ingest([lead()], MON_10AM)
    assert e.tick(MON_10AM) == []
    assert s.one("SELECT status FROM leads")["status"] == Status.RETRY.value


def test_watchdog_alerts_when_not_called():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    # engine "down" for 15 min: first tick happens late, but watchdog alerts once
    e.watchdog(MON_10AM + timedelta(minutes=15))
    e.watchdog(MON_10AM + timedelta(minutes=16))
    assert sum("no call was placed" in x[1] for x in n.sent) == 1


def test_stuck_call_is_failed():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    e.tick(MON_10AM)
    e.watchdog(MON_10AM + timedelta(minutes=20))
    assert s.one("SELECT status FROM leads")["status"] == Status.RETRY.value


def test_duplicate_event_ignored():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    ref = e.tick(MON_10AM)[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFERRED), MON_10AM)
    assert e.handle_event(CallEvent(ref, EventKind.NO_ANSWER), MON_10AM) == "duplicate_event"
    assert e.report() == {"TRANSFERRED": 1}


# ---- API
def test_api_flow():
    e, s, d, n = mk()
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    assert client.post("/webhooks/lead", json={}).status_code == 401
    r = client.post("/webhooks/lead", headers=h, json={
        "id": "x1", "full_name": "Asha", "phone_number": "p:+919876543210", "campus": "t.nagar_campus",
        "grade": "UKG", "student_name": "Nila", "created_time": MON_10AM.isoformat()})
    assert r.json()["new"] == 1 and len(d.calls) == 1
    assert d.calls[0].campus == "t_nagar"
    ref = d.calls[0].call_ref
    r = client.post("/webhooks/call-event", headers=h, json={"call_ref": ref, "kind": "transferred"})
    assert r.status_code == 200
    assert client.get("/report", headers=h).json()["summary"] == {"TRANSFERRED": 1}
    assert client.post("/webhooks/call-event", headers=h, json={"call_ref": ref, "kind": "bogus"}).status_code == 422


# ---- dialers
def test_failover_uses_backup(monkeypatch):
    from juvelcall.dialers import FailoverDialer, DialerError
    from juvelcall.dialers.base import Dialer

    class Bad(Dialer):
        def place_call(self, plan): raise DialerError("down")

    class Good(Dialer):
        def place_call(self, plan): return "ok1"

    e, s, d, n = mk()
    fd = FailoverDialer(Bad(), Good())
    e.dialer = fd
    e.ingest([lead()], MON_10AM)
    assert len(e.tick(MON_10AM)) == 1
    assert fd.failovers == 1
    assert s.one("SELECT provider_call_id p FROM calls")["p"] == "backup:ok1"


def test_volant_and_vapi_payloads():
    from juvelcall.dialers import VapiDialer
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    e.tick(MON_10AM)
    plan = d.calls[0]
    p = VapiDialer("k", "pn").build_payload(plan)
    assert p["customer"]["number"] == "+919876543210" and p["metadata"]["call_ref"] == plan.call_ref


def test_vapi_webhook_maps_results_and_syncs():
    e, s, d, n = mk()
    got = []
    e.sink = got.append
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    e.ingest([lead(), lead(key="2", phone="9811111111", campus="t_nagar_campus")], MON_10AM)
    e.tick(MON_10AM)
    r1, r2 = d.calls[0].call_ref, d.calls[1].call_ref
    VH = {"x-vapi-secret": "vapi-test"}
    assert client.post("/webhooks/vapi", json={}).status_code == 401
    rep = lambda ref, why: {"message": {"type": "end-of-call-report", "endedReason": why,
                                        "call": {"id": "v1", "metadata": {"call_ref": ref}}}}
    assert client.post("/webhooks/vapi", json=rep(r1, "assistant-forwarded-call"), headers=VH).json()["mapped"] == "TRANSFERRED"
    assert client.post("/webhooks/vapi", json=rep(r2, "customer-did-not-answer"), headers=VH).json()["mapped"] == "NO_ANSWER"
    assert client.post("/webhooks/vapi", json={"message": {"type": "status-update"}}, headers=VH).json()["ignored"]
    assert got[0]["outcome"] == "TRANSFERRED" and got[0]["event_type"] == "admissions_call_result"


def test_console_api():
    e, s, d, n = mk()
    client = TestClient(create_app(e, clock=lambda: MON_10AM + timedelta(minutes=3)))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    e.ingest([lead()], MON_10AM)
    ref = e.tick(MON_10AM + timedelta(minutes=1))[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFERRED), MON_10AM + timedelta(minutes=2))
    j = client.get("/api/console", headers=h).json()
    assert j["kpis"]["enquiries"] == 1 and j["kpis"]["transferred"] == 1
    assert j["kpis"]["called_within_5_min_pct"] == 100
    assert "•" in j["leads"][0]["phone"]            # phone is masked for the dashboard
    assert client.get("/api/lead/1", headers=h).json()["calls"][0]["outcome"] == "TRANSFERRED"
    assert client.get("/api/console").status_code == 401


def test_resolve_and_stale_is_silent():
    e, s, d, n = mk()
    e.ingest([lead(at=MON_10AM - timedelta(days=5))], MON_10AM)     # old lead, e.g. first sheet import
    assert e.report() == {"STALE": 1} and n.sent == []
    e.ingest([lead(key="2", phone="9811111112")], MON_10AM)
    ref = e.tick(MON_10AM)[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), MON_10AM)
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    assert client.post("/api/lead/2/resolve", headers=h, json={"note": "called back"}).json() == {"ok": True}
    assert s.one("SELECT status FROM leads WHERE id=2")["status"] == "RESOLVED"
    assert client.post("/api/lead/99/resolve", headers=h, json={}).status_code == 404


def test_overlapping_ticks_never_double_call():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    row = s.one("SELECT * FROM leads")
    first = e._place(row, MON_10AM)          # worker A claims
    second = e._place(row, MON_10AM)         # worker B holds a stale copy of the row
    assert first and second is None
    assert len(d.calls) == 1


def test_tick_endpoint_and_kv():
    e, s, d, n = mk()
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    e.ingest([lead()], MON_10AM)
    assert len(client.post("/admin/tick", headers=h).json()["placed"]) == 1
    s.kv_set("a", "1"); s.kv_set("a", "2")
    assert s.kv_get("a") == "2" and s.kv_get("zz") is None


def test_per_day_hours():
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from juvelcall.rules import in_hours, next_open
    tz = ZoneInfo("Asia/Kolkata")
    c = {"hours": {"mon": ["08:30", "14:30"], "sat": ["09:00", "14:00"]}, "days": [], "start": "", "end": ""}
    assert in_hours(datetime(2026, 11, 16, 8, 45, tzinfo=tz), c, tz)          # Mon 08:45
    assert not in_hours(datetime(2026, 11, 16, 14, 30, tzinfo=tz), c, tz)     # Mon 14:30 closed
    assert not in_hours(datetime(2026, 11, 14, 8, 45, tzinfo=tz), c, tz)      # Sat 08:45 before 09:00
    assert in_hours(datetime(2026, 11, 14, 13, 59, tzinfo=tz), c, tz)
    assert not in_hours(datetime(2026, 11, 17, 10, 0, tzinfo=tz), c, tz)      # Tue not listed
    assert next_open(datetime(2026, 11, 14, 15, 0, tzinfo=tz), c, tz) == datetime(2026, 11, 16, 8, 30, tzinfo=tz)


def test_alert_goes_to_campus_inbox():
    e, s, d, n = mk()
    e.cfg.campuses["ecr"]["alert_emails"] = ["ecr-inbox@example.com"]
    e.ingest([lead()], MON_10AM)
    row = s.one("SELECT id FROM leads ORDER BY id DESC LIMIT 1")
    e._alert(MON_10AM, "test", row["id"], "subj", "body")
    assert n.sent[-1][0] == ["ecr-inbox@example.com"]
