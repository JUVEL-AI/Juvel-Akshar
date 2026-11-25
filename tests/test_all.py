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
    import copy
    s, d, n = Store(), MockDialer(), MemoryNotifier()
    return Engine(Config(copy.deepcopy(cfg.data)), s, d, n), s, d, n   # own config copy: settings tests must not leak


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
    assert plan.greeting_text.startswith("Hello, this is an automated call from Akshar Arbol International School regarding your registration. This call may be recorded.")
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
    ops.snapshot_base(e); e._base["sources"]["default_campus"] = ""
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
    e._base["campuses"]["ecr"]["alert_emails"] = ["ecr-inbox@example.com"]
    e.ingest([lead()], MON_10AM)
    row = s.one("SELECT id FROM leads ORDER BY id DESC LIMIT 1")
    e._alert(MON_10AM, "test", row["id"], "subj", "body")
    assert n.sent[-1][0] == ["ecr-inbox@example.com"]


def test_webhook_notifier_posts_and_falls_back(monkeypatch):
    import requests
    from juvelcall.notify import WebhookNotifier, MemoryNotifier
    calls = []

    class R:  # fake response
        def __init__(self, code): self.status_code = code
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=0: (calls.append((url, json)), R(200))[1])
    assert WebhookNotifier("https://hook.example/x").send(["a@x.com"], "S", "B")
    assert calls[0][1]["subject"] == "S" and calls[0][1]["to"] == ["a@x.com"]
    monkeypatch.setattr(requests, "post", lambda *a, **k: R(500))
    fb = MemoryNotifier()
    assert WebhookNotifier("https://hook.example/x", fallback=fb).send(["a@x.com"], "S", "B")
    assert fb.sent


# ---- start / pause / stop
def test_pause_blocks_calls_and_resume_places_them():
    e, s, d, n = mk()
    e.set_control("pause", MON_10AM)
    e.ingest([lead()], MON_10AM)
    assert e.tick(MON_10AM) == [] and not d.calls                 # nothing dialed while paused
    assert s.one("SELECT status FROM leads")["status"] == "QUEUED"  # enquiry is kept
    e.set_control("start", MON_10AM + timedelta(minutes=30))
    assert len(e.tick(MON_10AM + timedelta(minutes=30))) == 1     # called after resume


def test_no_not_called_alert_while_paused():
    e, s, d, n = mk()
    e.set_control("pause", MON_10AM)
    n.sent.clear()
    e.ingest([lead()], MON_10AM)
    e.tick(MON_10AM + timedelta(minutes=30))
    assert not any("no call was placed" in x[1] for x in n.sent)


def test_stop_parks_waiting_leads_and_new_ones():
    e, s, d, n = mk()
    e.ingest([lead("1")], MON_10AM)
    res = e.set_control("stop", MON_10AM)
    assert res["parked"] == 1 and res["state"] == "stopped"
    e.ingest([lead("2", phone="9123456780")], MON_10AM)
    assert {r["status"] for r in s.q("SELECT status FROM leads")} == {"NEEDS_REVIEW"}
    e.set_control("start", MON_10AM + timedelta(minutes=5))
    assert e.tick(MON_10AM + timedelta(minutes=5)) == [] and not d.calls   # parked leads are NOT auto-called


def test_control_change_emails_admins_and_old_waiting_leads_go_stale():
    e, s, d, n = mk()
    e.set_control("pause", MON_10AM)
    assert any("paused" in x[1].lower() for x in n.sent)
    e.ingest([lead()], MON_10AM)
    e.set_control("start", MON_10AM + timedelta(days=3))
    e.tick(MON_10AM + timedelta(days=3))
    assert s.one("SELECT status FROM leads")["status"] == "STALE" and not d.calls


def test_control_api_needs_secret_and_validates():
    e, s, d, n = mk()
    c = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    assert c.post("/api/control", json={"action": "pause"}).status_code == 401
    assert c.post("/api/control", json={"action": "explode"}, headers=h).status_code == 422
    assert c.post("/api/control", json={"action": "pause"}, headers=h).json()["state"] == "paused"
    assert c.get("/api/console", headers=h).json()["control"]["state"] == "paused"


# ---- settings, availability, reports, reminders, export, follow-up
from juvelcall import ops


def test_settings_change_hours_and_validate():
    e, s, d, n = mk()
    with pytest.raises(ValueError):
        ops.save_settings(e, {"hours": {"mon": ["10:00", "09:00"]}}, MON_10AM, "t")
    with pytest.raises(ValueError):
        ops.save_settings(e, {"campuses": {"ecr": {"counselors": [{"name": "A", "number": "123"}]}}}, MON_10AM, "t")
    ops.save_settings(e, {"hours": {d: ["09:00", "12:00"] for d in ["mon", "tue", "wed", "thu", "fri"]}}, MON_10AM, "Priya")
    assert not in_hours(MON_10AM.replace(hour=13), e.c, IST) and in_hours(MON_10AM, e.c, IST)
    assert not in_hours(datetime(2026, 10, 24, 10, 0, tzinfo=IST), e.c, IST)        # Saturday now closed
    assert ops.hours_label(e.c) == "Mon–Fri 9:00–12:00"
    assert s.one("SELECT who FROM audit WHERE action='settings'")["who"] == "Priya"


def test_settings_survive_restart_and_change_counselor():
    e, s, d, n = mk()
    ops.save_settings(e, {"campuses": {"ecr": {"counselors": [{"name": "Meera", "number": "9840922615"}]}}}, MON_10AM, "t")
    e2 = Engine(Config(__import__("copy").deepcopy(cfg.data)), s, d, n)             # new process, same database
    e2.ingest([lead()], MON_10AM)
    e2.tick(MON_10AM)
    assert d.calls[-1].counselor_numbers == ["+919840922615"] and d.calls[-1].counselor_name == "Meera"


def test_no_counselor_on_duty_leaves_lead_waiting_and_alerts():
    e, s, d, n = mk()
    ops.save_settings(e, {"campuses": {"ecr": {"counselors": [{"name": "Vidya", "number": "7550033275", "on_duty": False}]}}},
                      MON_10AM, "t")
    e.ingest([lead()], MON_10AM)
    assert e.tick(MON_10AM) == [] and not d.calls
    e.tick(MON_10AM + timedelta(minutes=15))
    assert any("no call was placed" in x[1] for x in n.sent)
    ops.save_settings(e, {"campuses": {"ecr": {"counselors": [{"name": "Vidya", "number": "7550033275", "on_duty": True}]}}},
                      MON_10AM, "t")
    assert len(e.tick(MON_10AM + timedelta(minutes=16))) == 1


def test_daily_summary_sent_once_after_close():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    e.tick(MON_10AM)
    before = MON_10AM.replace(hour=14, minute=40)
    assert "daily_summary" not in ops.periodic(e, before)                              # closes 14:30 + 30 min
    after = MON_10AM.replace(hour=15, minute=5)
    assert ops.periodic(e, after).get("daily_summary") is True
    assert "daily_summary" not in ops.periodic(e, after + timedelta(minutes=1))
    assert any("summary, ECR" in x[1] for x in n.sent) and any("summary, T Nagar" in x[1] for x in n.sent)


def test_weekly_report_on_saturday_only():
    e, s, d, n = mk()
    sat = datetime(2026, 10, 24, 14, 40, tzinfo=IST)
    assert ops.periodic(e, sat).get("weekly_report") is True
    assert "weekly_report" not in ops.periodic(e, sat + timedelta(minutes=1))
    e2, *_ = mk()
    assert "weekly_report" not in ops.periodic(e2, MON_10AM.replace(hour=15, minute=5))


def test_callback_reminders_then_stop_after_done():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    ref = e.tick(MON_10AM)[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), MON_10AM)
    n.sent.clear()
    assert ops.periodic(e, MON_10AM + timedelta(minutes=30)).get("reminders") == 0
    assert ops.periodic(e, MON_10AM + timedelta(minutes=61)).get("reminders") == 1
    assert ops.periodic(e, MON_10AM + timedelta(minutes=62)).get("reminders") == 0      # not repeated
    assert ops.periodic(e, MON_10AM + timedelta(minutes=241)).get("reminders") == 1     # second reminder
    lid = s.one("SELECT id FROM leads")["id"]
    e.resolve(lid, MON_10AM + timedelta(minutes=242), who="Viji")
    assert ops.periodic(e, MON_10AM + timedelta(minutes=243)).get("reminders") == 0


def test_export_and_search():
    e, s, d, n = mk()
    e.ingest([lead(phone="9840922615")], MON_10AM)
    csvtext = ops.export_csv(e, None, MON_10AM)
    assert "+919840922615" in csvtext and csvtext.startswith("Time,Parent")
    assert ops.search(e, "0922615")[0]["phone"].startswith("+91")
    assert "9840922615" not in ops.search(e, "Asha")[0]["phone"]                          # masked in search
    assert ops.search(e, "ab") == []


def test_parent_followup_is_off_by_default_and_works_when_enabled():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM)
    ref = e.tick(MON_10AM)[0]
    e.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), MON_10AM)
    assert n.events == []
    e2, s2, d2, n2 = mk()
    e2.cfg.notifications["parent_followup"] = {"enabled": True, "text_callback": "Hi {parent_name}, {school_name} will call you back."}
    e2.ingest([lead()], MON_10AM)
    ref = e2.tick(MON_10AM)[0]
    e2.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), MON_10AM)
    assert n2.events[0][0] == "parent_message" and "will call you back" in n2.events[0][1]["text"]


def test_new_api_endpoints_need_secret():
    e, s, d, n = mk()
    c = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    for path in ["/api/settings", "/api/audit", "/api/search?q=abc", "/api/export"]:
        assert c.get(path).status_code == 401
        assert c.get(path, headers=h).status_code == 200
    r = c.put("/api/settings", json={"max_attempts": 9, "by": "x"}, headers=h)
    assert r.status_code == 422
    r = c.put("/api/settings", json={"max_attempts": 3, "by": "x"}, headers=h)
    assert r.json()["max_attempts"] == 3


def test_loop_guard_blocks_transfer_to_calling_number_and_settings_reject_it():
    e, s, d, n = mk()
    own = e.cfg.campuses["ecr"]["caller_id"] = "+917550033275"          # same as the ECR counselor
    e._base["campuses"]["ecr"]["caller_id"] = own
    e.ingest([lead()], MON_10AM)
    assert e.tick(MON_10AM) == [] and not d.calls
    assert any("would loop" in x[2] or "equals the calling number" in x[1] for x in n.sent)
    with pytest.raises(ValueError):
        ops.save_settings(e, {"campuses": {"ecr": {"counselors": [{"name": "X", "number": "7550033275"}]}}}, MON_10AM, "t")


# ---- the school's real sheet shapes
def test_parse_time_handles_meta_iso_and_sheet_slash_dates():
    from juvelcall.sources.csv_source import parse_time
    t = parse_time("2026-09-25T06:53:57+05:30", IST)
    assert t.hour == 6 and t.utcoffset().total_seconds() == 19800
    assert parse_time("2026-09-25T11:00:00-07:00", IST).utcoffset().total_seconds() == -25200
    m = parse_time("10/29/2026 11:34:20", IST)                      # month first, as in this sheet
    assert (m.month, m.day, m.hour) == (10, 29, 11)
    assert parse_time("29/10/2026 11:34:20", IST).month == 10        # first number above 12 can only be a day
    assert parse_time("03/04/2026 09:00:00", IST, "dmy").month == 4
    with pytest.raises(ValueError):
        parse_time("Shk Abdeali Al-Yamani", IST)


def test_sheet_rows_unreadable_time_become_stale_and_junk_rows_are_skipped():
    from juvelcall.sources.csv_source import lead_from_row
    cols = cfg.sources["csv_columns"]
    bad = lead_from_row({"Timing": "garbage", "full_name": "A", "phone_number": "p:+919840922615"}, cols, IST, "meta_sheet", "row 9")
    assert bad.created_at.year == 2000 and "_time_unreadable" in bad.raw
    with pytest.raises(ValueError):
        lead_from_row({"Timing": "", "full_name": "", "phone_number": ""}, cols, IST, "meta_sheet", "row 10")


def test_first_sheet_read_is_baseline_nobody_called_or_emailed():
    e, s, d, n = mk()
    fresh = lead(phone="9840922615")                                  # even a lead from minutes ago
    e.ingest([fresh], MON_10AM, baseline=True)
    assert s.one("SELECT status FROM leads")["status"] == "STALE"
    assert e.tick(MON_10AM) == [] and not d.calls and not n.sent


def test_old_bad_rows_do_not_email_but_fresh_ones_do():
    e, s, d, n = mk()
    old = lead(phone="notaphone"); old.created_at = MON_10AM - timedelta(days=30)
    e.ingest([old], MON_10AM)
    assert not n.sent
    e.ingest([lead(key="2", phone="notaphone")], MON_10AM)
    assert any("needs attention" in x[1] for x in n.sent)


def test_campus_column_with_grade_or_either_and_default_campus():
    e, s, d, n = mk()
    ops.snapshot_base(e); e._base["sources"]["default_campus"] = ""
    l1 = lead(); l1.campus = "grade-9"; l1.grade_text = ""
    e.ingest([l1], MON_10AM)
    r = s.one("SELECT status, grade FROM leads")
    assert r["status"] == "NEEDS_REVIEW" and r["grade"] == "9th grade"      # class level rescued, campus needs a person
    e2, s2, d2, n2 = mk()
    ops.snapshot_base(e2); e2._base["sources"]["default_campus"] = "t_nagar"
    l2 = lead(phone="9840922616"); l2.campus = "either"
    e2.ingest([l2], MON_10AM)
    assert s2.one("SELECT campus, status FROM leads")["campus"] == "t_nagar"


def test_whisper_without_student_but_with_class():
    e, s, d, n = mk()
    l = lead(); l.student_name = ""; l.grade_text = "grade 5"
    e.ingest([l], MON_10AM); e.tick(MON_10AM)
    assert "class 5th grade" in d.calls[-1].whisper_text and "student" not in d.calls[-1].whisper_text


def test_default_campus_ecr_only_for_open_answers():
    e, s, d, n = mk()
    assert e.cfg.sources["default_campus"] == "ecr"
    a = lead(key="a", phone="9811111111"); a.campus = "either"
    b = lead(key="b", phone="9822222222"); b.campus = ""
    c = lead(key="c", phone="9833333333"); c.campus = "Somewhere"
    e.ingest([a, b, c], MON_10AM)
    rows = {r["external_key"] if "external_key" in r.keys() else r["phone"]: r for r in s.q("SELECT * FROM leads")}
    camp = [(r["campus"], r["status"]) for r in rows.values()]
    assert camp.count(("ecr", "QUEUED")) == 2 and any(st == "NEEDS_REVIEW" for _, st in camp)


def test_integrations_saved_from_dashboard():
    import pytest as _p
    e, s, d, n = mk()
    ops.snapshot_base(e)
    v = ops.save_integrations(e, {"vapi_api_key": "sk_live_abcdef123456", "vapi_phone_number_id": "pn_123456789",
                                  "vapi_server_secret": "srv-secret-9999", "caller_id": "+914440001111"}, MON_10AM, "juvel")
    assert v["live"] and v["vapi_api_key"]["value"].endswith("3456") and "sk_live" not in v["vapi_api_key"]["value"]
    assert v["caller_id"]["value"] == "+914440001111" and e.cfg.dialer["vapi"]["api_key"] == "sk_live_abcdef123456"
    assert e.dialer.__class__.__name__ == "VapiDialer"
    assert all(c["caller_id"] == "+914440001111" for c in e.cfg.campuses.values())
    assert s.one("SELECT action,detail FROM audit ORDER BY id DESC")["action"] == "integrations"
    assert "sk_live" not in s.one("SELECT detail FROM audit ORDER BY id DESC")["detail"]
    # blank keeps, other fields change, the secret survives a later tick
    ops.save_integrations(e, {"caller_id": "+914440002222"}, MON_10AM, "juvel")
    e.tick(MON_10AM)
    assert e.cfg.dialer["vapi"]["api_key"] == "sk_live_abcdef123456" and e.cfg.campuses["ecr"]["caller_id"] == "+914440002222"
    with _p.raises(ValueError):
        ops.save_integrations(e, {"caller_id": "7550033275"}, MON_10AM, "juvel")      # a counselor's own number
    with _p.raises(ValueError):
        ops.save_integrations(e, {"vapi_api_key": "short"}, MON_10AM, "juvel")
    ops.save_integrations(e, {"clear": ["vapi_api_key"]}, MON_10AM, "juvel")
    assert ops.integrations_view(e)["vapi_api_key"]["from"] != "dashboard"


def test_vapi_payload_styles():
    from juvelcall.dialers import VapiDialer
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    plan = d.calls[0]
    b = VapiDialer("k", "pn").build_payload(plan)["assistant"]                   # default = the blind style tested live
    tool = b["model"]["tools"][0]
    dest = tool["destinations"][0]
    assert dest["transferPlan"]["mode"] == "blind-transfer" and dest["transferPlan"]["sipVerb"] == "dial"
    assert dest["transferPlan"]["fallbackPlan"]["message"] == plan.fallback_text
    assert dest["message"] == plan.greeting_text and "ECR campus" in plan.greeting_text and "Ms" not in "" and plan.counselor_name in plan.greeting_text
    assert plan.greeting_text.startswith("Hello, this is an automated call from Akshar Arbol International School regarding your registration. This call may be recorded. Your counselor from the admissions team will speak with you now. Please stay on the line.")
    assert tool["messages"][0] == {"type": "request-start", "content": plan.greeting_text, "blocking": True}
    assert "firstMessage" not in b and b["firstMessageMode"].startswith("assistant-speaks-first-with")
    a = VapiDialer("k", "pn", transfer_style="assistant").build_payload(plan)["assistant"]["model"]["tools"][0]["destinations"][0]
    assert a["transferPlan"]["mode"] == "warm-transfer-experimental" and a["transferPlan"]["transferAssistant"]["firstMessage"].endswith("Shall I connect the parent now?")
    ta = a["transferPlan"]["transferAssistant"]
    assert "Connecting now" not in ta["firstMessage"] and "transferSuccessful" in ta["model"]["messages"][0]["content"] and "transferCancel" in ta["model"]["messages"][0]["content"]
    old = VapiDialer("k", "pn", transfer_style="legacy").build_payload(plan)["assistant"]
    assert old["firstMessage"] == plan.greeting_text and old["model"]["tools"][0]["destinations"][0]["transferPlan"]["mode"] == "warm-transfer-say-message"


def test_vapi_falls_back_when_new_settings_rejected(monkeypatch):
    from juvelcall.dialers import VapiDialer
    import requests
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    plan = d.calls[0]
    seen = []
    class R:
        def __init__(s, c, t="", j=None): s.status_code, s.text, s._j, s.content = c, t, j or {}, b"x"
        def json(s): return s._j
    def fake(url, json=None, headers=None, timeout=None):
        seen.append(json["assistant"].get("firstMessage"))
        return R(400, "bad transferPlan") if len(seen) == 1 else R(201, "", {"id": "call-1"})
    monkeypatch.setattr(requests, "post", fake)
    assert VapiDialer("k", "pn").place_call(plan) == "call-1" and seen[0] is None and seen[1] == plan.greeting_text   # blind refused -> legacy


def test_vapi_voice_from_dashboard():
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    plan = d.calls[0]
    ops.snapshot_base(e)
    ops.save_integrations(e, {"vapi_voice_id": "abcDEF123voice"}, MON_10AM, "juvel")
    assert e.cfg.dialer["vapi"]["voice_id"] == "abcDEF123voice"
    from juvelcall.dialers import VapiDialer
    pl = VapiDialer("k", "pn", voice_id="abcDEF123voice").build_payload(plan)["assistant"]
    assert pl["voice"]["provider"] == "11labs" and pl["voice"]["voiceId"] == "abcDEF123voice"
    assert "voice" not in VapiDialer("k", "pn").build_payload(plan)["assistant"]


def test_call_now_outside_hours_and_delete_lead():
    SUN = datetime(2026, 11, 15, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))          # a Sunday: closed
    e, s, d, n = mk()
    e.ingest([lead(at=SUN)], SUN)
    assert e.tick(SUN) == [] and not d.calls                                     # normal rules: no call
    lid = s.one("SELECT id FROM leads")["id"]
    assert e.call_now(lid, SUN, "juvel") == ""
    assert len(d.calls) == 1 and s.one("SELECT action FROM audit ORDER BY id DESC")["action"] == "call_now"
    assert s.one("SELECT COUNT(*) c FROM kv WHERE k LIKE 'force_%'")["c"] == 0
    s.x("UPDATE leads SET status='NO_RESPONSE' WHERE id=?", (lid,))
    e.set_control("pause", SUN, "juvel")
    assert "Start" in e.call_now(lid, SUN, "juvel")
    # delete lets the same phone be entered again
    e.set_control("start", SUN, "juvel")
    s.x("UPDATE leads SET status='NO_RESPONSE' WHERE id=?", (lid,))
    assert e.delete_lead(lid, SUN, "juvel") == ""
    assert s.one("SELECT COUNT(*) c FROM leads")["c"] == 0 and s.one("SELECT COUNT(*) c FROM calls")["c"] == 0
    e.ingest([lead(at=SUN)], SUN)
    assert s.one("SELECT COUNT(*) c FROM leads")["c"] == 1
    s.x("UPDATE leads SET status='CALLING'")
    assert "progress" in e.delete_lead(s.one("SELECT id FROM leads")["id"], SUN, "juvel")
    assert "progress" in e.call_now(s.one("SELECT id FROM leads")["id"], SUN, "juvel")


def test_vapi_forwarding_failure_is_not_a_transfer():
    e, s, d, n = mk()
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    ref = d.calls[0].call_ref
    r = client.post("/webhooks/vapi", headers={"x-vapi-secret": "vapi-test"}, json={"message": {
        "type": "end-of-call-report", "endedReason": "call.forwarding.no-answer", "call": {"id": "x", "metadata": {"call_ref": ref}}}})
    assert r.json()["mapped"] == "TRANSFER_FAILED"


def test_call_records_saved_and_shown():
    e, s, d, n = mk()
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    ref = d.calls[0].call_ref
    client.post("/webhooks/vapi", headers={"x-vapi-secret": "vapi-test"}, json={"message": {
        "type": "end-of-call-report", "endedReason": "assistant-forwarded-call", "durationSeconds": 41.6, "cost": 0.04,
        "artifact": {"recordingUrl": "https://example.com/r.wav", "transcript": "AI: Hello"},
        "analysis": {"summary": "Parent was connected."}, "call": {"id": "v1", "metadata": {"call_ref": ref}}}})
    from juvelcall import console
    out = console.build(e, MON_10AM)
    c = out["leads"][0]["calls"][0]
    assert c["recording"] == "https://example.com/r.wav" and c["seconds"] == 42 and "connected" in c["summary"] and "Hello" in c["transcript"]
    ops.snapshot_base(e)
    assert ops.save_integrations(e, {"vapi_transfer_style": "assistant"}, MON_10AM, "juvel")["vapi_transfer_style"]["value"] == "assistant"


def test_records_visibility_setting():
    e, s, d, n = mk()
    from juvelcall import console
    ops.snapshot_base(e)
    assert console.build(e, MON_10AM)["records_staff"] is False
    ops.save_settings(e, {"records_staff": True}, MON_10AM, "juvel")
    assert console.build(e, MON_10AM)["records_staff"] is True and ops.effective_settings(e)["records_staff"] is True


def test_sheet_sources_from_dashboard(monkeypatch):
    import json as _j
    from juvelcall import sheets_admin as sa
    heads = ["Timing", "ad_name", "campaign_name", "which_campus_are_you_enquiring_for?", "which_school_is_your_child_currently_studying_in?",
             "which_grade_is_your_child_currently_studying_in?", "full_name", "phone_number", "city", "email", "Lead Quality ", "Status"]
    g = sa.suggest_columns(heads)
    assert g["phone"] == "phone_number" and g["name"] == "full_name" and g["campus"] == "which_campus_are_you_enquiring_for?"
    assert g["grade"] == "which_grade_is_your_child_currently_studying_in?" and g["created_at"] == "Timing" and g["email"] == "email"
    assert sa.parse_sheet_id("https://docs.google.com/spreadsheets/d/1hqnfahxY-agKd8NiJ4l3lpz8Q4W3DzRjUYQdhJO0iLY/edit?gid=0#gid=0").startswith("1hqnfahxY")
    with pytest.raises(ValueError):
        sa.parse_sheet_id("hello")

    e, s, d, n = mk()
    ops.snapshot_base(e)
    key = _j.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com", "private_key": "x"})
    v = ops.save_integrations(e, {"google_service_account": key}, MON_10AM, "juvel")
    assert v["google_service_account"]["value"] == "bot@proj.iam.gserviceaccount.com" and "private_key" not in str(v)
    with pytest.raises(ValueError):
        ops.save_integrations(e, {"google_service_account": "nope"}, MON_10AM, "juvel")

    cols = {"phone": "phone_number", "name": "full_name", "campus": "which_campus_are_you_enquiring_for?", "created_at": "Timing"}
    link = "https://docs.google.com/spreadsheets/d/1hqnfahxY-agKd8NiJ4l3lpz8Q4W3DzRjUYQdhJO0iLY/edit"
    with pytest.raises(ValueError):
        sa.save(e, {"sheets": [{"spreadsheet": link, "worksheet": "Sheet1", "columns": {"phone": "phone_number"}}]}, MON_10AM, "juvel")
    out = sa.save(e, {"sheets": [{"label": "Leads", "spreadsheet": link, "worksheet": "Sheet1", "columns": cols}], "default_campus": "ecr"}, MON_10AM, "juvel")
    assert out["sheets"][0]["spreadsheet_id"].startswith("1hqnfahxY") and out["default_campus"] == "ecr"

    rows = {"v": [{"Timing": "2026-11-16T10:00:00+05:30", "full_name": "Old Parent", "phone_number": "p:+919811111111",
                   "which_campus_are_you_enquiring_for?": "ecr_campus"}]}
    monkeypatch.setattr(sa, "read_rows", lambda creds, sid, ws: rows["v"])
    r1 = sa.poll_all(e, MON_10AM)
    assert r1["Leads"].get("baseline") is True and s.one("SELECT status FROM leads")["status"] == "STALE"        # already there: not called
    rows["v"].append({"Timing": "2026-11-16T10:05:00+05:30", "full_name": "New Parent", "phone_number": "p:+919822222222",
                      "which_campus_are_you_enquiring_for?": "either"})
    r2 = sa.poll_all(e, MON_10AM)
    assert s.one("SELECT status, campus FROM leads WHERE name='New Parent'")["campus"] == "ecr"                    # 'either' -> default campus


def test_voice_speed_setting():
    from juvelcall.dialers import VapiDialer
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    plan = d.calls[0]
    assert VapiDialer("k", "pn", voice_id="v123456789").build_payload(plan)["assistant"]["voice"]["speed"] == 0.9
    assert VapiDialer("k", "pn", voice_id="v123456789", voice_speed=5).build_payload(plan)["assistant"]["voice"]["speed"] == 1.2
    ops.snapshot_base(e)
    assert ops.save_integrations(e, {"vapi_voice_speed": "0.85"}, MON_10AM, "juvel")["vapi_voice_speed"]["value"] == "0.85"
    with pytest.raises(ValueError):
        ops.save_integrations(e, {"vapi_voice_speed": "3"}, MON_10AM, "juvel")


def test_transfer_email_control_email_and_hold_music():
    from juvelcall.dialers import VapiDialer
    e, s, d, n = mk()
    e.ingest([lead()], MON_10AM); e.tick(MON_10AM)
    ref = d.calls[0].call_ref
    n.sent.clear()
    e.handle_event(CallEvent(ref, EventKind.TRANSFERRED, "v1", "+917550033275", ""), MON_10AM)
    to, subj, body = n.sent[-1][0], n.sent[-1][1], n.sent[-1][2]
    assert "Parent connected" in subj and "Asha" in body and "class: LKG" in body and to == e.cfg.campuses["ecr"]["alert_emails"]
    n.sent.clear()
    e.set_control("pause", MON_10AM, "juvel")
    assert n.sent[-1][0] == ["bragadeeshsundararajan@gmail.com"]                  # not the school inboxes
    plan = d.calls[0]
    dest = VapiDialer("k", "pn", transfer_style="assistant", hold_audio_url="https://x.example/hold.mp3").build_payload(plan)["assistant"]["model"]["tools"][0]["destinations"][0]
    assert dest["transferPlan"]["holdAudioUrl"] == "https://x.example/hold.mp3"
    ops.snapshot_base(e)
    with pytest.raises(ValueError):
        ops.save_integrations(e, {"vapi_hold_audio_url": "http://x/y.mp3"}, MON_10AM, "juvel")
    assert ops.save_integrations(e, {"vapi_hold_audio_url": "https://x.example/hold.mp3"}, MON_10AM, "juvel")["vapi_hold_audio_url"]["value"].endswith("hold.mp3")


# ---- privacy / security hardening ----
def test_greeting_discloses_automated_and_recorded():
    e, *_ = mk()
    g = e.cfg.messages["greeting"]
    assert "automated call" in g and "may be recorded" in g


def test_purge_erases_recordings_and_old_leads_and_asks_provider():
    e, s, d, n = mk()
    erased = []
    d.delete_call_data = lambda pid: erased.append(pid) or True
    e.cfg.data["privacy"] = {"recording_days": 30, "lead_days": 180}
    e.ingest([lead("p1")], MON_10AM)
    row = s.one("SELECT id FROM leads")
    old = (MON_10AM - timedelta(days=40)).astimezone(timezone.utc).isoformat()
    s.x("INSERT INTO calls(call_ref,lead_id,attempt,started_at,provider_call_id,recording_url,transcript) VALUES(?,?,?,?,?,?,?)",
        ("c1", row["id"], 1, old, "prov-1", "https://rec", "hello"))
    out = e.purge_old(MON_10AM)
    assert out["recordings"] == 1 and erased == ["prov-1"]
    assert s.one("SELECT recording_url,transcript FROM calls WHERE call_ref='c1'")["recording_url"] == ""
    s.x("UPDATE leads SET updated_at=? WHERE id=?", ((MON_10AM - timedelta(days=200)).astimezone(timezone.utc).isoformat(), row["id"]))
    assert e.purge_old(MON_10AM)["leads"] == 1 and s.one("SELECT 1 FROM leads") is None


def test_delete_lead_asks_provider_to_erase_recording():
    e, s, d, n = mk()
    erased = []
    d.delete_call_data = lambda pid: erased.append(pid) or True
    e.ingest([lead("p2")], MON_10AM)
    rid = s.one("SELECT id FROM leads")["id"]
    s.x("INSERT INTO calls(call_ref,lead_id,attempt,started_at,provider_call_id) VALUES(?,?,?,?,?)", ("c2", rid, 1, "2026-10-19T04:00:00+00:00", "prov-2"))
    assert e.delete_lead(rid, MON_10AM, "admin") == "" and erased == ["prov-2"]


def test_integrations_encrypted_at_rest_when_data_key_set(monkeypatch):
    monkeypatch.setenv("DATA_KEY", "unit-test-key")
    s = Store()
    s.kv_set("integrations", '{"vapi_api_key":"sk_live_secret"}')
    raw = s.one("SELECT v FROM kv WHERE k='integrations'")["v"]
    assert raw.startswith("enc:") and "sk_live_secret" not in raw
    assert s.kv_get("integrations") == '{"vapi_api_key":"sk_live_secret"}'
    monkeypatch.delenv("DATA_KEY")
    assert s.kv_get("integrations") is None          # no key: never leak ciphertext


def test_health_is_minimal_without_secret_and_secret_compare_works():
    e, *_ = mk()
    e.cfg.security["webhook_secret"] = "s3cret-value"
    t = TestClient(create_app(e, clock=lambda: MON_10AM))
    assert t.get("/health").json().get("leads") is None
    assert t.get("/health", headers={"X-Webhook-Secret": "s3cret-value"}).json().get("leads") is not None
    assert t.get("/api/audit", headers={"X-Webhook-Secret": "wrong"}).status_code == 401
    assert t.get("/api/audit", headers={"X-Webhook-Secret": "s3cret-value"}).status_code == 200


# ---- the school's real sheets (shapes taken from their exports)
def test_real_campus_and_grade_values():
    for raw, want in [("t_nagar_campus", "t_nagar"), ("t.nagar_campus", "t_nagar"), ("tng", "t_nagar"), ("T.Nagar", "t_nagar"),
                      ("ecr_injambakkam_campus", "ecr"), ("ecr_campus", "ecr"), ("Injambakkam", "ecr"), ("ecr", "ecr"),
                      ("either", None), ("either_of_them", None), ("", None)]:
        assert canonical_campus(raw) == want, raw
    for raw, want in [("elc-1", "ELC 1"), ("grade-9", "9th grade"), ("grade_1", "1st grade"), ("pre_kg", "Pre-KG"), ("Std 4", "4th grade"),
                      ("toddler", "Toddler"), ("Toddler / Preschool", "Preschool"), ("IB DP", "IB Diploma"), ("AS/A Levels", "AS and A Levels"),
                      ("as-a-levels", "AS and A Levels"), ("Euro kids", ""), ("Grades 1-5", ""), ("3rd", "3rd grade"), ("Ukg", "UKG")]:
        assert clean_grade(raw) == want, raw


def test_meta_sheet_swapped_school_and_grade_columns():
    from juvelcall import sheets_admin
    heads = ["Timing", "ad_name", "campaign_name", "which_campus_are_you_enquiring_for?", "which_school_is_your_child_currently_studying_in?",
             "which_grade_is_your_child_currently_studying_in?", "full_name", "phone_number", "city", "email", "Lead Quality ", "Remarks on Parents"]
    cols = sheets_admin.suggest_columns(heads)
    assert cols["name"] == "full_name" and cols["phone"] == "phone_number" and cols["campus"].startswith("which_campus")
    assert cols["grade"].startswith("which_grade") and cols["grade_alt"].startswith("which_school")
    from juvelcall.sources.csv_source import lead_from_row
    row = {"Timing": "2026-11-16T18:10:19+05:30", "which_campus_are_you_enquiring_for?": "ecr_injambakkam_campus",
           "which_school_is_your_child_currently_studying_in?": "Grade 6", "which_grade_is_your_child_currently_studying_in?": "Chettinad vidhyashram",
           "full_name": "Revathi", "phone_number": "p:+919600025990"}
    lead_ = lead_from_row(row, cols, IST, "t", "r2")
    e, s, d, n = mk()
    e.ingest([lead_], MON_10AM)
    r = s.one("SELECT campus,grade,phone FROM leads")
    assert r["campus"] == "ecr" and r["grade"] == "6th grade" and r["phone"] == "+919600025990"


def test_google_ads_and_website_tabs_suggestions():
    from juvelcall import sheets_admin
    c = sheets_admin.suggest_columns(["Timestamp", "Branch", "Student Name", "Class", "Phone", "Parent Name", "Message", "Quality ", "Remarks on Parents"])
    assert (c["phone"], c["campus"], c["grade"], c["student_name"], c["name"]) == ("Phone", "Branch", "Class", "Student Name", "Parent Name")
    w = sheets_admin.suggest_columns(["Timestamp", "Child Name", "Date of Birth", "Current Class", "Father Name", "Contact No", "Email", "Admission Class", "Campus Preference", "Current School"])
    assert (w["phone"], w["grade"], w["student_name"], w["name"], w["campus"]) == ("Contact No", "Admission Class", "Child Name", "Father Name", "Campus Preference")
    m = sheets_admin.suggest_columns(["id", "created_time", "ad_name", "adset_name", "campaign_name", "form_name", "grade", "campus", "email", "full_name", "phone_number"])
    assert m["name"] == "full_name"


def test_school_presets_offered_and_valid():
    e, *_ = mk()
    v = __import__("juvelcall.sheets_admin", fromlist=["view"]).view(e)
    assert len(v["presets"]) == 5 and sum(1 for p in v["presets"] if p["enabled"]) == 3
    from juvelcall import sheets_admin
    out = sheets_admin.validate(e, {"sheets": [{**p, "spreadsheet": p["spreadsheet_id"]} for p in v["presets"]], "default_campus": "ecr", "date_order": "mdy"})
    assert len(out["sheets"]) == 5


# ---- test lab
def test_test_lab_is_isolated_from_real_data():
    e, s, d, n = mk()
    e.ingest([lead("real1", phone="9876543210")], MON_10AM)
    lid, why = e.create_test_lead(MON_10AM, "Tester", "Kid", "grade-3", "9000000001", "ecr", "9000000002", "juvel")
    assert why == "" and lid
    plan = d.calls[-1]
    assert plan.parent_number == "+919000000001" and plan.counselor_numbers == ["+919000000002"] and plan.counselor_name == "Test Counselor"
    # never in real lists, counts, reports or search
    import juvelcall.console as console, juvelcall.ops as ops
    assert all(l["parent"] != "Tester" for l in console.build(e, MON_10AM)["leads"])
    assert "Tester" in [l["parent"] for l in console.build(e, MON_10AM, test=True)["leads"]]
    assert sum(e.report().values()) == 1
    assert ops.search(e, "Tester") == [] and ops.search(e, "9000000001") == []
    # no emails to the school for a test, even on transfer
    before = len(n.sent)
    e.handle_event(CallEvent(plan.call_ref, EventKind.TRANSFERRED, transferred_to="+919000000002"), MON_10AM)
    assert len(n.sent) == before
    assert e.delete_test_data(MON_10AM, "juvel") == 1 and s.one("SELECT 1 FROM leads WHERE source='test'") is None
    assert s.one("SELECT 1 FROM leads WHERE source<>'test'") is not None      # real data untouched


def test_test_lab_validation_and_api():
    e, s, d, n = mk()
    assert e.create_test_lead(MON_10AM, "T", "", "", "9000000001", "ecr", "9000000001", "x")[1].startswith("Use two different")
    assert e.create_test_lead(MON_10AM, "T", "", "", "abc", "ecr", "9000000002", "x")[0] is None
    assert e.create_test_lead(MON_10AM, "T", "", "", "9000000001", "nowhere", "9000000002", "x")[1] == "Choose a campus"
    client = TestClient(create_app(e, clock=lambda: MON_10AM))
    h = {"X-Webhook-Secret": cfg.security["webhook_secret"]}
    r = client.post("/api/test/lead", json={"name": "A", "parent_phone": "9000000001", "counselor_phone": "9000000002", "campus": "ecr", "grade": "LKG"}, headers=h)
    assert r.status_code == 200
    assert len(client.get("/api/test", headers=h).json()["leads"]) == 1
    assert client.delete("/api/test", headers=h).json()["deleted"] == 1
    assert client.get("/api/test").status_code == 401


def test_test_call_goes_out_even_when_real_calling_is_paused_and_is_never_retried():
    e, s, d, n = mk()
    e.set_control("pause", MON_10AM, "juvel")
    lid, why = e.create_test_lead(MON_10AM, "T", "", "", "9000000001", "ecr", "9000000002", "juvel")
    assert why == "" and len(d.calls) == 1
    e.handle_event(CallEvent(d.calls[-1].call_ref, EventKind.NO_ANSWER), MON_10AM)
    e.set_control("start", MON_10AM, "juvel")
    e.tick(MON_10AM + timedelta(hours=3))
    assert len(d.calls) == 1                      # the test is not retried by the real dialer loop


# ---- start-from date per sheet, staff control
def test_start_from_date_calls_newer_rows_even_if_old_and_paces_them():
    from datetime import datetime
    e, s, d, n = mk()
    since = datetime(2026, 10, 8, tzinfo=e.cfg.tz)
    rows = [lead(f"x{i}", phone=f"98765432{i:02d}", at=MON_10AM - timedelta(days=10 - (i % 3)), campus=("tng" if i % 2 else "ecr_campus")) for i in range(5)]
    rows.append(lead("old", phone="9876500000", at=MON_10AM - timedelta(days=40)))     # before the date: silent
    out = e.ingest(rows, MON_10AM, since=since)
    assert out["new"] == 6
    assert s.one("SELECT status FROM leads WHERE key='old'")["status"] == "STALE"
    assert s.one("SELECT COUNT(*) n FROM leads WHERE status='QUEUED' AND allow_old=1")["n"] == 5
    placed = e.tick(MON_10AM)
    assert len(placed) == 2                                   # paced: backfill_per_minute (one per campus line)
    s.x("UPDATE leads SET status='NO_RESPONSE' WHERE status='CALLING'")      # both calls ended
    assert len(e.tick(MON_10AM + timedelta(minutes=1))) == 2


def test_start_from_change_revives_skipped_rows_and_validation():
    from datetime import datetime
    from juvelcall import sheets_admin
    e, s, d, n = mk()
    since = datetime(2026, 10, 15, tzinfo=e.cfg.tz)
    e.ingest([lead("a", phone="9876543201", at=MON_10AM - timedelta(days=12))], MON_10AM, since=since)
    assert s.one("SELECT status FROM leads WHERE key='a'")["status"] == "STALE"
    assert e.revive_since("t", datetime(2026, 10, 1, tzinfo=e.cfg.tz), MON_10AM) in (0, 1)
    with pytest.raises(ValueError):
        sheets_admin.validate(e, {"sheets": [{"spreadsheet": "1" * 30, "worksheet": "S", "start_from": "10/11/2026", "columns": {"phone": "P", "created_at": "T"}}]})


def test_staff_can_start_pause_stop_through_dashboard_route_source():
    src = open("web/app/api/control/route.ts").read()
    assert "needAdmin" not in src and "whoFromReq" in src


# ---- one Vobiz number per campus
def test_each_campus_calls_from_its_own_vapi_number():
    from juvelcall.dialers import VapiDialer, DialerError
    from juvelcall import ops
    e, s, d, n = mk()
    d2 = VapiDialer("k" * 12, "default-id-1", phone_number_ids={"ecr": "ecr-id-111", "t_nagar": "tng-id-222"})
    e.ingest([lead("a", phone="9876543201", campus="ecr_campus"), lead("b", phone="9876543202", campus="t_nagar_campus")], MON_10AM)
    rows = {r["campus"]: r for r in s.q("SELECT * FROM leads")}
    assert d2.build_payload(e.build_plan(rows["ecr"]))["phoneNumberId"] == "ecr-id-111"
    assert d2.build_payload(e.build_plan(rows["t_nagar"]))["phoneNumberId"] == "tng-id-222"
    d3 = VapiDialer("k" * 12, "", phone_number_ids={"ecr": "ecr-id-111"})
    with pytest.raises(DialerError):
        d3.build_payload(e.build_plan(rows["t_nagar"]))               # a campus with no number never silently uses the wrong one
    assert VapiDialer("k" * 12, "only-id-1").build_payload(e.build_plan(rows["t_nagar"]))["phoneNumberId"] == "only-id-1"


def test_per_campus_numbers_save_and_apply():
    from juvelcall import ops
    e, s, d, n = mk()
    v = ops.save_integrations(e, {"vapi_api_key": "k" * 12, "vapi_phone_id_ecr": "ecr-id-111", "vapi_phone_id_t_nagar": "tng-id-222",
                                  "caller_id_ecr": "+918064264030", "caller_id_t_nagar": "+918064264099"}, MON_10AM, "juvel")
    assert v["live"] and v["vapi_phone_id_t_nagar"]["value"] == "tng-id-222"
    assert e.cfg.dialer["vapi"]["phone_number_ids"] == {"ecr": "ecr-id-111", "t_nagar": "tng-id-222"}
    assert e.cfg.campuses["t_nagar"]["caller_id"] == "+918064264099" and e.cfg.campuses["ecr"]["caller_id"] == "+918064264030"
    assert e.dialer.phone_number_ids["t_nagar"] == "tng-id-222"
    with pytest.raises(ValueError):
        ops.save_integrations(e, {"caller_id_t_nagar": "+917305972752"}, MON_10AM, "juvel")      # a counselor's own number


def test_website_form_formats():
    from datetime import timezone
    from juvelcall.sources.csv_source import parse_time
    from juvelcall.normalize import clean_grade
    t = parse_time("November 17, 2026", timezone.utc)
    assert (t.year, t.month, t.day) == (2026, 11, 17)
    assert parse_time("2026-10-05 9:05:03", timezone.utc).hour == 9
    assert clean_grade("E2") == "ELC 2"
    assert clean_grade("TD") == "Toddler"
    assert clean_grade("G6") == "6th grade"


def test_backfill_window_calls_only_that_range_and_new_rows():
    from datetime import datetime
    e, s, d, n = mk()
    rows = [lead("may", phone="9876500001", at=datetime(2026, 5, 10, 11, tzinfo=e.cfg.tz)),
            lead("jul", phone="9876500002", at=datetime(2026, 7, 31, 11, tzinfo=e.cfg.tz)),
            lead("sep", phone="9876500003", at=datetime(2026, 9, 10, 11, tzinfo=e.cfg.tz)),
            lead("apr", phone="9876500004", at=datetime(2026, 4, 30, 11, tzinfo=e.cfg.tz))]
    e.ingest(rows, MON_10AM, baseline=True)
    out = e.revive_range("t", datetime(2026, 5, 1, tzinfo=e.cfg.tz), datetime(2026, 7, 31, tzinfo=e.cfg.tz), MON_10AM)
    st = lambda k: s.one("SELECT status FROM leads WHERE key=?", (k,))["status"]
    assert out["queued"] == 2 and st("may") == "QUEUED" and st("jul") == "QUEUED"
    assert st("sep") == "STALE" and st("apr") == "STALE"
    e.ingest([lead("new", phone="9876500005", at=MON_10AM)], MON_10AM)
    assert st("new") == "QUEUED"


def test_first_read_is_bulk_and_repeat_reads_are_cheap():
    e, s, d, n = mk()
    rows = [lead(f"k{i}", phone=f"98765{i:05d}", at=MON_10AM - timedelta(days=3)) for i in range(50)]
    assert e.ingest(rows, MON_10AM, baseline=True)["new"] == 50
    assert s.one("SELECT COUNT(*) n FROM leads WHERE status='STALE' AND note LIKE 'already in the sheet%'")["n"] == 50
    again = e.ingest(rows + [lead("fresh", phone="9876599999", at=MON_10AM)], MON_10AM)
    assert again["new"] == 1 and again["skipped_existing"] == 50
    assert s.one("SELECT status FROM leads WHERE key='fresh'")["status"] == "QUEUED"


def test_older_enquiries_are_paced_per_minute_and_get_two_tries():
    from datetime import datetime
    e, s, d, n = mk()
    since = datetime(2026, 10, 8, tzinfo=e.cfg.tz)
    rows = [lead(f"p{i}", phone=f"98765433{i:02d}", at=MON_10AM - timedelta(days=5), campus=("tng" if i % 2 else "ecr_campus")) for i in range(6)]
    e.ingest(rows, MON_10AM, since=since)
    assert len(e.tick(MON_10AM)) == 2
    assert len(e.tick(MON_10AM + timedelta(seconds=20))) == 0          # another tick inside the same minute adds nothing
    s.x("UPDATE leads SET status='NO_RESPONSE' WHERE status='CALLING'")
    assert len(e.tick(MON_10AM + timedelta(seconds=70))) == 2


def test_dashboard_lists_older_enquiries_called_today():
    from datetime import datetime
    from juvelcall import console
    e, s, d, n = mk()
    since = datetime(2026, 10, 8, tzinfo=e.cfg.tz)
    e.ingest([lead("old1", phone="9876544001", at=MON_10AM - timedelta(days=9)),
              lead("new1", phone="9876544002", at=MON_10AM - timedelta(minutes=1))], MON_10AM, since=since)
    e.tick(MON_10AM)
    out = console.build(e, MON_10AM)
    older = [l for l in out["leads"] if l["older"]]
    assert len(older) == 1 and out["kpis"]["older_called"] == 1 and out["kpis"]["enquiries"] == 1


def test_dashboard_shows_who_is_on_the_line_now():
    from juvelcall import console
    e, s, d, n = mk()
    e.ingest([lead("live1", phone="9876545001", student="Riya")], MON_10AM)
    e.tick(MON_10AM)
    out = console.build(e, MON_10AM)
    assert [x["student"] for x in out["live"]] == ["Riya"]
    assert console.build(e, MON_10AM + timedelta(minutes=30))["live"] == []     # a stuck 'calling' row does not show forever


def test_search_can_be_limited_to_one_campus_and_live_shows_source():
    from juvelcall import console, ops
    e, s, d, n = mk()
    e.ingest([lead("a", phone="9876546001", campus="ecr_campus"), lead("b", phone="9876546002", campus="tng")], MON_10AM)
    assert len(ops.search(e, "asha")) == 2
    only = ops.search(e, "asha", campus="ecr")
    assert len(only) == 1 and only[0]["campus"] == "ECR"
    e.tick(MON_10AM)
    assert all("source" in x for x in console.build(e, MON_10AM)["live"])


def test_call_backs_go_one_at_a_time():
    e, s, d, n = mk()
    ids = []
    for i in range(3):
        e.ingest([lead(f"cb{i}", phone=f"98765470{i:02d}")], MON_10AM)
    for r in s.q("SELECT id FROM leads"):
        s.x("UPDATE leads SET status='ANSWERED_NOT_TRANSFERRED' WHERE id=?", (r["id"],))
        ids.append(r["id"])
    res = e.recall(ids, MON_10AM, "vidya")
    assert res["queued"] == 3 and res["started"] == 1 and res["waiting"] == 2
    assert len(d.calls) == 1                                   # the other two wait
    assert e.tick(MON_10AM + timedelta(seconds=30)) == []      # first call still in progress
    first = s.one("SELECT id FROM leads WHERE status='CALLING'")["id"]
    s.x("UPDATE leads SET status='TRANSFERRED' WHERE id=?", (first,))   # the call ended
    assert len(e.tick(MON_10AM + timedelta(seconds=60))) == 1
    assert e.recall_cancel(MON_10AM + timedelta(seconds=61), "vidya") == 1
    s.x("UPDATE leads SET status='TRANSFERRED' WHERE status='CALLING'")
    assert e.tick(MON_10AM + timedelta(seconds=90)) == []


def test_each_campus_has_its_own_start_pause_stop():
    e, s, d, n = mk()
    e.ingest([lead("ecr1", phone="9876547001", campus="ecr_campus"), lead("tng1", phone="9876547002", campus="tng")], MON_10AM)
    e.set_campus_control("t_nagar", "pause", MON_10AM, "viji")
    e.tick(MON_10AM)
    st = lambda k: s.one("SELECT status FROM leads WHERE key=?", (k,))["status"]
    assert st("ecr1") != "QUEUED" and st("tng1") == "QUEUED"             # ECR called, T Nagar waits
    assert e.control_info()["campuses"]["t_nagar"]["state"] == "paused" and e.control_info()["campuses"]["ecr"]["state"] == "running"
    e.set_campus_control("t_nagar", "start", MON_10AM + timedelta(minutes=1), "viji")
    e.tick(MON_10AM + timedelta(minutes=1))
    assert st("tng1") != "QUEUED"
    e.set_campus_control("ecr", "stop", MON_10AM + timedelta(minutes=2), "vidya")
    e.ingest([lead("ecr2", phone="9876547003", campus="ecr_campus", at=MON_10AM + timedelta(minutes=2))], MON_10AM + timedelta(minutes=2))
    assert st("ecr2") == "NEEDS_REVIEW"
    assert e.can_call("t_nagar") and not e.can_call("ecr")
    e.set_control("pause", MON_10AM + timedelta(minutes=3), "priya")           # the master switch overrides every campus
    assert not e.can_call("t_nagar")


def test_calling_card_announces_before_and_lingers_after():
    from juvelcall import console
    e, s, d, n = mk()
    e.ingest([lead("cc1", phone="9876548001")], MON_10AM)
    before = console.build(e, MON_10AM - timedelta(seconds=1))["live"]
    e.tick(MON_10AM)
    during = console.build(e, MON_10AM)["live"]
    assert [x["phase"] for x in during] == ["calling"]
    lid = during[0]["id"]
    e.resolve(lid, MON_10AM + timedelta(seconds=30), "x", "t")
    assert [x["phase"] for x in console.build(e, MON_10AM + timedelta(seconds=40))["live"]] == ["recent"]
    assert console.build(e, MON_10AM + timedelta(minutes=10))["live"] == []
    assert all(x["phase"] in ("next", "calling", "recent") for x in before)


def test_huge_dashboard_answers_are_trimmed():
    import json
    from juvelcall import console
    big = {"leads": [{"id": i, "calls": [{"transcript": "x" * 6000, "summary": "y" * 500, "detail": "z" * 500, "outcome": "ok"}]} for i in range(900)]}
    out = console.trim(big)
    assert len(json.dumps(out)) <= 3_000_000
    assert out["leads"][0]["calls"][0]["transcript"] == "x" * 6000 and out["leads"][899]["calls"][0]["transcript"] == ""
    assert out["leads"][899]["calls"][0]["outcome"] == "ok"


def test_older_enquiries_wait_while_the_campus_line_is_busy():
    from datetime import datetime
    e, s, d, n = mk()
    since = datetime(2026, 10, 8, tzinfo=e.cfg.tz)
    e.ingest([lead(f"b{i}", phone=f"98765490{i:02d}", at=MON_10AM - timedelta(days=5), campus="tng") for i in range(4)], MON_10AM, since=since)
    assert len(e.tick(MON_10AM)) == 1                                  # one at a time for T Nagar, not two
    assert len(e.tick(MON_10AM + timedelta(minutes=1))) == 0           # the first call is still on the line
    first = s.one("SELECT id FROM leads WHERE status='CALLING'")["id"]
    e.resolve(first, MON_10AM + timedelta(minutes=2), "x", "t")
    assert len(e.tick(MON_10AM + timedelta(minutes=2, seconds=5))) == 1  # line free again


def test_campus_switches_cost_one_query_and_big_ranges_finish_over_several_minutes():
    import time
    from datetime import datetime
    e, s, d, n = mk()
    cnt = {"n": 0}
    orig = s.q
    def counting(*a, **k):
        cnt["n"] += 1
        return orig(*a, **k)
    s.q = counting
    for _ in range(50):
        e.campus_state("ecr")
    assert cnt["n"] <= 1
    s.q = orig
    since = datetime(2026, 10, 8, tzinfo=e.cfg.tz)
    e.ingest([lead(f"r{i}", phone=f"98765510{i:02d}", at=MON_10AM - timedelta(days=5)) for i in range(5)], MON_10AM, baseline=True)
    out = e.revive_range("t", datetime(2026, 10, 1, tzinfo=e.cfg.tz), datetime(2026, 10, 30, tzinfo=e.cfg.tz), MON_10AM, deadline=time.monotonic() - 1)
    assert out.get("partial") and out["queued"] == 0


def test_new_enquiries_also_wait_for_the_campus_line_and_go_before_older_ones():
    from datetime import datetime
    e, s, d, n = mk()
    e.ingest([lead(f"n{i}", phone=f"98765520{i:02d}", at=MON_10AM, campus="tng") for i in range(3)], MON_10AM)
    assert len(e.tick(MON_10AM)) == 1                                  # three new enquiries, one call at a time
    assert len(e.tick(MON_10AM + timedelta(seconds=30))) == 0
    first = s.one("SELECT id FROM leads WHERE status='CALLING'")["id"]
    e.resolve(first, MON_10AM + timedelta(seconds=40), "x", "t")
    assert len(e.tick(MON_10AM + timedelta(seconds=45))) == 1
