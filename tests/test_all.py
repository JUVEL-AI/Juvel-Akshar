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
    e.cfg.sources["default_campus"] = ""
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
    e.cfg.sources["default_campus"] = ""
    l1 = lead(); l1.campus = "grade-9"; l1.grade_text = ""
    e.ingest([l1], MON_10AM)
    r = s.one("SELECT status, grade FROM leads")
    assert r["status"] == "NEEDS_REVIEW" and r["grade"] == "9th grade"      # class level rescued, campus needs a person
    e2, s2, d2, n2 = mk()
    e2.cfg.sources["default_campus"] = "t_nagar"
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
    new = VapiDialer("k", "pn").build_payload(plan)["assistant"]
    tool = new["model"]["tools"][0]
    assert tool["transferPlan"]["mode"] == "warm-transfer-experimental"
    assert tool["transferPlan"]["transferAssistant"]["firstMessage"] == plan.whisper_text
    assert tool["messages"][0]["content"] == plan.greeting_text and "firstMessage" not in new
    assert tool["destinations"][0]["sipVerb"] == "dial" and new["firstMessageMode"].startswith("assistant-speaks-first-with")
    old = VapiDialer("k", "pn", transfer_style="legacy").build_payload(plan)["assistant"]
    assert old["firstMessage"] == plan.greeting_text and old["model"]["tools"][0]["destinations"][0]["transferPlan"]["mode"] == "warm-transfer-say-message"
    wait = VapiDialer("k", "pn", immediate_transfer=False).build_payload(plan)["assistant"]
    assert wait["firstMessage"] == plan.greeting_text and wait["model"]["tools"][0]["transferPlan"]["mode"] == "warm-transfer-experimental"


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
