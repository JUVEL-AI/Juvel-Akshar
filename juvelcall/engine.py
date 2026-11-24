from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone

from .config import Config
from .dialers.base import Dialer, DialerError
from .models import CallEvent, CallPlan, EventKind, RawLead, Status
from .normalize import canonical_campus, clean_grade, clean_name, clean_phone
from . import ops
from .notify import Notifier
from .rules import in_hours, next_open
from .store import Store, iso, parse

ACTIVE_CALL_STATES = (Status.CALLING.value,)


class Engine:
    def __init__(self, cfg: Config, store: Store, dialer: Dialer, notifier: Notifier,
                 webhook_url: str | None = None, sink=None):
        self.sink = sink
        self.cfg, self.store, self.dialer, self.notifier = cfg, store, dialer, notifier
        self.webhook_url = webhook_url
        self.c = cfg.calling
        ops.snapshot_base(self)

    def _on_duty(self, campus_key: str):
        """First counselor of the campus marked on duty (set from the dashboard Settings page), or None."""
        for c in self.cfg.campuses[campus_key]["counselors"]:
            if c.get("on_duty", True):
                return c
        return None

    def _parent_followup(self, lead, kind: str, now) -> None:
        """Optional message to the parent (e.g. WhatsApp sent by Make). Off unless notifications.parent_followup.enabled."""
        pf = (self.cfg.notifications or {}).get("parent_followup") or {}
        if not pf.get("enabled") or not lead["phone"]:
            return
        text = (pf.get("text_callback") if kind == "callback" else pf.get("text_no_response")) or ""
        if text:
            self.notifier.post_event("parent_message", {
                "channel": pf.get("channel", "whatsapp"), "to": lead["phone"], "parent": lead["name"] or "",
                "text": text.format(school_name=self.cfg.messages["school_name"], parent_name=lead["name"] or "Parent")})

    # ---------- start / pause / stop ----------
    CONTROL_LABEL = {"running": "Running", "paused": "Paused", "stopped": "Stopped"}

    def control_state(self) -> str:
        v = self.store.kv_get("control")
        return v if v in self.CONTROL_LABEL else "running"

    def control_info(self) -> dict:
        return {"state": self.control_state(), "changed_at": self.store.kv_get("control_changed_at") or "",
                "by": self.store.kv_get("control_by") or ""}

    def set_control(self, action: str, now: datetime, who: str = "dashboard") -> dict:
        """start = calls go out.  pause = no new calls; enquiries still saved and queued, called on start.
        stop = no calls; everything waiting is parked for a person (Needs review); new enquiries are parked too.
        Calls already ringing are not cut off."""
        new = {"start": "running", "pause": "paused", "stop": "stopped"}.get(action)
        if new is None:
            raise ValueError("action must be start, pause or stop")
        old = self.control_state()
        parked = 0
        if new == "stopped":
            for r in self.store.q("SELECT id FROM leads WHERE source<>'test' AND status IN (?,?)", (Status.QUEUED.value, Status.RETRY.value)):
                self.store.update_lead(r["id"], now, status=Status.NEEDS_REVIEW.value, next_attempt_at=None,
                                       note="calling was stopped by an admin; please follow up by hand")
                parked += 1
        if new == "running" and old != "running":
            # leads that waited while paused are called now (the 48 h stale rule still applies in tick)
            self.store.x("UPDATE leads SET next_attempt_at=? WHERE status IN (?,?) AND next_attempt_at>?",
                         (iso(now), Status.QUEUED.value, Status.RETRY.value, iso(now)))
        self.store.kv_set("control", new)
        self.store.kv_set("control_changed_at", iso(now))
        self.store.kv_set("control_by", who)
        self.store.add_audit(now, who, f"calling_{new}", f"{old} -> {new}" + (f", parked {parked}" if parked else ""))
        if new != old:
            extra = f" {parked} waiting enquiries were moved to Needs review." if parked else ""
            self._alert(now, f"control_{new}", None, f"Calling {self.CONTROL_LABEL[new].lower()} ({who})",
                        f"Automatic calling is now {self.CONTROL_LABEL[new].upper()}, set from the {who} at "
                        f"{now.astimezone(self.cfg.tz):%d %b %H:%M}.{extra}",
                        to=self.cfg.notifications.get("control_emails") or None)
        return self.control_info() | {"parked": parked}

    # ---------- intake ----------
    def ingest(self, leads: list[RawLead], now: datetime, baseline: bool = False, since: datetime | None = None) -> dict:
        """baseline=True is the very first read of the school's sheet: every row already there is stored as STALE,
        so nobody who enquired before go-live is called or emailed about."""
        ops.apply_overrides(self)
        out = {"new": 0, "skipped_existing": 0}
        for lead in sorted(leads, key=lambda l: l.created_at):
            if self.store.one("SELECT 1 FROM leads WHERE source=? AND key=?", (lead.source, lead.key)):
                out["skipped_existing"] += 1
                continue
            self._ingest_one(lead, now, baseline, since)
            out["new"] += 1
        return out

    def _ingest_one(self, lead: RawLead, now: datetime, baseline: bool = False, since: datetime | None = None) -> int:
        phone = clean_phone(lead.phone)
        campus = canonical_campus(lead.campus)
        grade = clean_grade(lead.grade_text)
        if not campus:
            # This school's Meta form sometimes puts a class level ('grade-9', 'elc-1') or 'either' in the campus column.
            grade = grade or clean_grade(lead.campus)
            raw = (lead.campus or "").strip().lower()
            gradelike = bool(clean_grade(lead.campus))
            open_answer = (not raw) or gradelike or any(w in raw for w in ("either", "any", "both", "no preference", "not sure", "none", "n/a"))
            # only an open answer ('either', blank, a class level) takes the default; an unreadable campus still needs a person
            campus = (self.cfg.sources.get("default_campus") or None) if open_answer else None
        status, note, next_at = Status.QUEUED, "", None

        # 'start from date' (set per sheet on the dashboard): older rows stay silent, newer ones are called even if they
        # are more than 48 hours old
        catch_up = bool(since and lead.created_at >= since)
        if baseline:
            status, note = Status.STALE, "already in the sheet before go-live"
        elif since and lead.created_at < since:
            status, note = Status.STALE, "before the start date chosen for this sheet"
        elif not phone:
            status, note = Status.INVALID_PHONE, f"could not read phone '{lead.phone}'"
        elif self.store.is_dnc(phone):
            status, note = Status.DO_NOT_CALL, "on do-not-call list"
        elif self._is_duplicate(phone, lead.created_at):
            status, note = Status.DUPLICATE, "same phone already enquired recently"
        elif campus not in self.cfg.campuses:
            status, note = Status.NEEDS_REVIEW, f"unknown campus '{lead.campus}'"
        elif not catch_up and now - lead.created_at > timedelta(hours=self.c["stale_after_hours"]):
            status, note = Status.STALE, "lead too old for automatic call"
        elif self.control_state() == "stopped":
            status, note = Status.NEEDS_REVIEW, "arrived while calling was stopped; please follow up by hand"
        else:
            next_at = next_open(max(now, lead.created_at), self.c, self.cfg.tz)

        lead_id = self.store.insert(
            """INSERT INTO leads(source,key,row_ref,created_at,received_at,name,student_name,phone,phone_raw,
               campus,campus_raw,grade,grade_raw,email,city,status,attempts,next_attempt_at,note,raw,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
            (lead.source, lead.key, lead.row_ref, iso(lead.created_at), iso(now), clean_name(lead.name),
             clean_name(lead.student_name), phone, lead.phone, campus, lead.campus, grade, lead.grade_text,
             lead.email, lead.city, status.value, iso(next_at) if next_at else None, note,
             self.store.dump_raw(lead.raw), iso(now)))
        if catch_up and status == Status.QUEUED:
            self.store.update_lead(lead_id, now, allow_old=1)
        fresh = now - lead.created_at <= timedelta(hours=self.c["stale_after_hours"])
        if (status in (Status.INVALID_PHONE, Status.NEEDS_REVIEW) and fresh
                and "while calling was stopped" not in note):                  # old rows (first sheet import) stay silent
            self._alert(now, "lead_needs_attention", lead_id,
                        f"Admissions lead needs attention ({status.value})",
                        f"Lead #{lead_id}: {clean_name(lead.name)} / {lead.phone} / {lead.campus}\n{note}\n"
                        "No automatic call was made. Please follow up manually.")
        return lead_id

    def _is_duplicate(self, phone: str, created_at: datetime) -> bool:
        since = iso(created_at - timedelta(hours=self.c["dedupe_window_hours"]))
        return self.store.one(
            "SELECT 1 FROM leads WHERE phone=? AND created_at>=? AND status NOT IN (?,?,?)",
            (phone, since, Status.DUPLICATE.value, Status.INVALID_PHONE.value, Status.DO_NOT_CALL.value)) is not None

    # ---------- calling ----------
    def tick(self, now: datetime, limit: int = 50) -> list[str]:
        """Place every call that is due. Returns call_refs placed."""
        placed = []
        ops.apply_overrides(self)
        if self.control_state() != "running":
            self.watchdog(now)               # only closes stuck calls while paused/stopped
            return placed
        rows = self.store.q(
            "SELECT * FROM leads WHERE source<>'test' AND status IN (?,?) AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (Status.QUEUED.value, Status.RETRY.value, iso(now), limit))
        pace = int(self.c.get("backfill_per_minute", 3) or 3)       # older enquiries (start-date catch-up) go out a few a minute
        caught_up = 0
        for r in rows:
            made = parse(r["created_at"])
            if r["allow_old"] and caught_up >= pace:
                continue
            if (made and not r["allow_old"] and now - made > timedelta(hours=self.c["stale_after_hours"])
                    and self.store.kv_get(f"force_{r['id']}") is None):
                self.store.update_lead(r["id"], now, status=Status.STALE.value, next_attempt_at=None,
                                       note="waited too long (calling was paused or the system was down)")
                continue
            forced = self.store.kv_get(f"force_{r['id']}") is not None
            if not forced and not in_hours(now, self.c, self.cfg.tz):
                self.store.update_lead(r["id"], now, next_attempt_at=iso(next_open(now, self.c, self.cfg.tz)))
                continue
            if not self._on_duty(r["campus"]):
                continue                 # nobody on duty for this campus: lead waits, the watchdog emails after 10 min
            ref = self._place(r, now)
            if ref:
                placed.append(ref)
                if r["allow_old"]:
                    caught_up += 1
        self.watchdog(now)
        return placed

    def revive_since(self, source: str, since: datetime, now: datetime) -> int:
        """The start date was moved earlier: rows that were skipped as 'before the start date' and fall after it are queued."""
        n = 0
        for r in self.store.q("SELECT id,created_at FROM leads WHERE source=? AND status=? AND note LIKE ?",
                              (source, Status.STALE.value, "before the start date%")):
            made = parse(r["created_at"])
            if made and made >= since:
                self.store.update_lead(r["id"], now, status=Status.QUEUED.value, allow_old=1, note="",
                                       next_attempt_at=iso(next_open(max(now, made), self.c, self.cfg.tz)))
                n += 1
        return n

    def revive_range(self, source: str, d_from: datetime, d_to: datetime, now: datetime) -> dict:
        """One-off catch-up asked for by the school: enquiries dated d_from..d_to (inclusive) that were skipped as old are
        queued and called at the paced backfill rate. Bad phones, do-not-call numbers, unknown campuses and phones already
        enquired or called elsewhere are left alone."""
        end = d_to + timedelta(days=1)
        done = {"queued": 0, "skipped": 0}
        taken: set[str] = set()
        live = (Status.STALE.value, Status.DUPLICATE.value, Status.INVALID_PHONE.value, Status.DO_NOT_CALL.value, Status.NEEDS_REVIEW.value)
        rows = self.store.q("SELECT * FROM leads WHERE source=? AND status=? AND (note LIKE ? OR note LIKE ? OR note LIKE ?) ORDER BY created_at",
                            (source, Status.STALE.value, "already in the sheet%", "before the start date%", "lead too old%"))
        for r in rows:
            made = parse(r["created_at"])
            if not made or made < d_from or made >= end:
                continue
            phone = clean_phone(r["phone"] or "")
            campus = canonical_campus(r["campus"] or "") or ""
            if campus not in self.cfg.campuses:
                grade_or_open = (r["campus"] or "").strip().lower()
                campus = (self.cfg.sources.get("default_campus") or "") if (not grade_or_open or "either" in grade_or_open) else ""
            busy = phone and self.store.one("SELECT 1 FROM leads WHERE phone=? AND id<>? AND status NOT IN (?,?,?,?,?)",
                                            (phone, r["id"], *live)) is not None
            if not phone or self.store.is_dnc(phone) or campus not in self.cfg.campuses or busy or phone in taken:
                done["skipped"] += 1
                continue
            taken.add(phone)
            self.store.update_lead(r["id"], now, status=Status.QUEUED.value, allow_old=1, note="", campus=campus,
                                   next_attempt_at=iso(next_open(now, self.c, self.cfg.tz)))
            done["queued"] += 1
        return done

    def build_plan(self, r) -> CallPlan:
        campus = self.cfg.campuses[r["campus"]]
        counselor = self._on_duty(r["campus"]) or campus["counselors"][0]
        test_num = self.store.kv_get(f"testnum_{r['id']}") if r["source"] == "test" else None
        if test_num:                      # Test lab: ring the tester's own phone, never a real counselor
            counselor = {**counselor, "name": "Test Counselor", "numbers": [test_num]}
        m = self.cfg.messages
        ctx = dict(school_name=m["school_name"], counselor_name=counselor["name"],
                   campus_label=campus["label"], student_name=r["student_name"], grade=r["grade"],
                   parent_name=r["name"] or "name not given")
        if r["student_name"] and r["grade"]:
            whisper = m["whisper_full"].format(**ctx)
        elif r["student_name"]:
            whisper = m["whisper_no_class"].format(**ctx)
        elif r["grade"]:
            whisper = m.get("whisper_class_only", m["whisper_minimal"]).format(**ctx)
        else:
            whisper = m["whisper_minimal"].format(**ctx)
        return CallPlan(
            call_ref=f"c-{uuid.uuid4().hex[:12]}", lead_id=r["id"], campus=r["campus"],
            parent_number=r["phone"], caller_id=campus["caller_id"],
            greeting_text=m["greeting"].format(**ctx), counselor_name=counselor["name"],
            counselor_numbers=list(counselor["numbers"]) + ([] if test_num else list(campus.get("backup_numbers", []))),
            counselor_ring_seconds=campus.get("counselor_ring_seconds", 25),
            whisper_text=whisper, fallback_text=m["fallback"].format(**ctx),
            max_call_seconds=self.c["max_call_seconds"], webhook_url=self.webhook_url)

    def _place(self, r, now: datetime) -> str | None:
        plan = self.build_plan(r)
        # Loop guard: never transfer to the very number we call from.
        own = clean_phone(plan.caller_id) if plan.caller_id else None
        if own and any(clean_phone(n) == own for n in plan.counselor_numbers):
            if not self.store.kv_get("loop_alerted_" + now.astimezone(self.cfg.tz).strftime("%Y%m%d")):
                self.store.kv_set("loop_alerted_" + now.astimezone(self.cfg.tz).strftime("%Y%m%d"), "1")
                self._alert(now, "loop_guard", None, "Calling stopped: counselor number equals the calling number",
                            "A counselor or backup number is the same as the number calls are made from, so the transfer "
                            "would loop. No calls are placed for that campus until it is fixed in Settings.")
            return None
        attempt = r["attempts"] + 1
        # Atomic claim: only one worker can move a lead to CALLING (serverless ticks can overlap).
        claimed = self.store.x(
            "UPDATE leads SET status=?, attempts=?, updated_at=? WHERE id=? AND status IN (?,?) AND attempts=?",
            (Status.CALLING.value, attempt, iso(now), r["id"], Status.QUEUED.value, Status.RETRY.value, r["attempts"]))
        if claimed.rowcount != 1:
            return None
        self.store.x("INSERT INTO calls(call_ref,lead_id,attempt,started_at) VALUES(?,?,?,?)",
                     (plan.call_ref, r["id"], attempt, iso(now)))
        try:
            pid = self.dialer.place_call(plan)
            self.store.x("UPDATE calls SET provider_call_id=? WHERE call_ref=?", (pid, plan.call_ref))
            return plan.call_ref
        except DialerError as e:
            self.handle_event(CallEvent(plan.call_ref, EventKind.FAILED, detail=str(e)), now)
            return None

    # ---------- results ----------
    def handle_event(self, ev: CallEvent, now: datetime) -> str:
        call = self.store.one("SELECT * FROM calls WHERE call_ref=?", (ev.call_ref,))
        if not call:
            return "unknown_call"
        lead = self.store.one("SELECT * FROM leads WHERE id=?", (call["lead_id"],))
        if ev.kind == EventKind.ANSWERED:
            self.store.x("UPDATE calls SET outcome=? WHERE call_ref=?", (ev.kind.value, ev.call_ref))
            return "ok"
        if call["ended_at"]:
            return "duplicate_event"          # providers resend webhooks; ignore repeats
        self.store.x("UPDATE calls SET outcome=?, detail=?, ended_at=? WHERE call_ref=?",
                     (ev.kind.value, ev.detail, iso(now), ev.call_ref))

        if ev.kind == EventKind.TRANSFERRED:
            self.store.update_lead(lead["id"], now, status=Status.TRANSFERRED.value,
                                   last_outcome="TRANSFERRED", transferred_to=ev.transferred_to,
                                   next_attempt_at=None)
            camp = self.cfg.campuses.get(lead["campus"]) or {}
            who = (self._on_duty(lead["campus"]) or {}).get("name", "the counselor")
            self._alert(now, "transferred", lead["id"], f"Parent connected to {who}: {lead['name'] or 'name not given'} ({camp.get('label', lead['campus'])})",
                        self._lead_summary(lead) + f"\nThe parent was connected to {who} at {now.astimezone(self.cfg.tz):%d %b %H:%M}.")
        elif ev.kind == EventKind.TRANSFER_FAILED:
            self.store.update_lead(lead["id"], now, status=Status.ANSWERED_NOT_TRANSFERRED.value,
                                   last_outcome="TRANSFER_FAILED", next_attempt_at=None)
            counselors = self.cfg.campuses[lead["campus"]]["counselors"]
            self._alert(now, "transfer_failed", lead["id"],
                        f"CALL BACK NEEDED: parent answered but no counselor reached ({lead['campus']})",
                        self._lead_summary(lead) + "\nParent heard the fallback message and expects a call back. "
                        "Please call this parent as soon as possible.",
                        to=self.cfg.notifications.get("admin_emails", []))
            self._parent_followup(lead, "callback", now)
        else:   # NO_ANSWER / BUSY / FAILED
            attempts = lead["attempts"]
            if attempts >= self.c["max_attempts"]:
                self.store.update_lead(lead["id"], now, status=Status.NO_RESPONSE.value,
                                       last_outcome=ev.kind.value, next_attempt_at=None)
                self._alert(now, "no_response", lead["id"],
                            f"Parent not reachable after {attempts} attempts",
                            self._lead_summary(lead) + "\nAll automatic attempts used. Please try manually.")
                self._parent_followup(lead, "no_response", now)
            else:
                delays = self.c["retry_minutes"]
                delay = delays[min(attempts - 1, len(delays) - 1)]
                nxt = next_open(now + timedelta(minutes=delay), self.c, self.cfg.tz)
                self.store.update_lead(lead["id"], now, status=Status.RETRY.value,
                                       last_outcome=ev.kind.value, next_attempt_at=iso(nxt))
        self._sync(lead["id"], ev.kind.value)
        return "ok"

    def _sync(self, lead_id: int, outcome: str) -> None:
        if not self.sink:
            return
        r = self.store.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        self.sink({"event_type": "admissions_call_result", "lead_id": lead_id, "outcome": outcome,
                   "status": r["status"], "attempts": r["attempts"], "parent_name": r["name"],
                   "student_name": r["student_name"], "phone": r["phone"], "campus": r["campus"],
                   "grade": r["grade"], "transferred_to": r["transferred_to"]})

    # ---------- safety net ----------
    def watchdog(self, now: datetime) -> None:
        """1) inquiry arrived, still not called -> alert once.  2) call stuck with no result -> fail it."""
        limit = timedelta(minutes=self.c["alert_if_not_called_after_minutes"])
        running = self.control_state() == "running"
        for r in (self.store.q("SELECT * FROM leads WHERE status=? AND alerted_not_called=0", (Status.QUEUED.value,))
                  if running else []):
            due = parse(r["next_attempt_at"])
            if due and now - due > limit and in_hours(now, self.c, self.cfg.tz):
                self.store.update_lead(r["id"], now, alerted_not_called=1)
                self._alert(now, "not_called", r["id"], "Inquiry received but no call was placed",
                            self._lead_summary(r) + f"\nWaiting since {due.astimezone(self.cfg.tz):%d %b %H:%M}.")
        stuck = timedelta(seconds=self.c["max_call_seconds"] + 120)
        for call in self.store.q("SELECT * FROM calls WHERE ended_at IS NULL"):
            started = parse(call["started_at"])
            if started and now - started > stuck:
                self.handle_event(CallEvent(call["call_ref"], EventKind.FAILED, detail="no result from provider"), now)

    # ---------- helpers ----------
    def _lead_summary(self, r) -> str:
        return (f"Lead #{r['id']} | parent: {r['name'] or '-'} | student: {r['student_name'] or '-'} | "
                f"class: {r['grade'] or '-'} | phone: {r['phone'] or r['phone_raw']} | campus: {r['campus_raw']}")

    def _alert(self, now, kind, lead_id, subject, body, to=None):
        aid = self.store.add_alert(now, kind, lead_id, subject, body)
        if lead_id:
            src = self.store.one("SELECT source FROM leads WHERE id=?", (lead_id,))
            if src and src["source"] == "test":
                return                    # test enquiries never email the school's officers
        if not to and lead_id:
            row = self.store.one("SELECT campus FROM leads WHERE id=?", (lead_id,))
            camp = self.cfg.campuses.get(row["campus"]) if row and row["campus"] else None
            to = (camp or {}).get("alert_emails") or None     # that campus's own admissions inbox
        ok = self.notifier.send(to or self.cfg.notifications.get("admin_emails", []), subject, body)
        if ok:
            self.store.x("UPDATE alerts SET sent=1 WHERE id=?", (aid,))

    def call_now(self, lead_id: int, now: datetime, who: str = "") -> str:
        """Admin: try this lead again right now, even outside calling hours. Returns '' on success or a reason it was refused."""
        r = self.store.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not r:
            return "No such enquiry"
        if r["status"] == Status.CALLING.value:
            return "A call to this parent is in progress"
        if not r["phone"] or not r["campus"]:
            return "This enquiry has no valid phone number or campus"
        if self.store.is_dnc(r["phone"]):
            return "This number is on the do-not-call list"
        if self.control_state() != "running":
            return "Calling is paused or stopped. Press Start first"
        self.store.update_lead(lead_id, now, status=Status.QUEUED.value, attempts=0, next_attempt_at=iso(now),
                               note=f"call started by {who or 'an admin'}")
        self.store.kv_set(f"force_{lead_id}", iso(now))
        self.store.add_audit(now, who, "call_now", f"lead {lead_id}")
        self.tick(now)
        self.store.x("DELETE FROM kv WHERE k=?", (f"force_{lead_id}",))
        return ""

    def delete_lead(self, lead_id: int, now: datetime, who: str = "") -> str:
        """Admin: remove an enquiry and its call records, so the same number can be tried again. Returns '' or a reason."""
        r = self.store.one("SELECT status FROM leads WHERE id=?", (lead_id,))
        if not r:
            return "No such enquiry"
        if r["status"] == Status.CALLING.value:
            return "A call to this parent is in progress"
        self._erase_lead(lead_id)
        self.store.add_audit(now, who, "delete_lead", f"lead {lead_id}")
        return ""

    # ---------- test lab (super admin) ----------
    def create_test_lead(self, now: datetime, name: str, student: str, grade: str, parent_phone: str,
                         campus: str, counselor_phone: str, who: str = "") -> tuple[int | None, str]:
        """A sandbox enquiry: rings parent_phone (the tester's own phone) and transfers to counselor_phone (a second phone
        of the tester). Never rings real parents or counselors, never emails the school, never shows in reports."""
        p, c = clean_phone(parent_phone), clean_phone(counselor_phone)
        if not p:
            return None, "The 'parent' phone is not a valid number"
        if not c:
            return None, "The 'counselor' phone is not a valid number"
        if p == c:
            return None, "Use two different phones: one plays the parent, the other plays the counselor"
        if campus not in self.cfg.campuses:
            return None, "Choose a campus"
        lead_id = self.store.insert(
            """INSERT INTO leads(source,key,row_ref,created_at,received_at,name,student_name,phone,phone_raw,
               campus,campus_raw,grade,grade_raw,email,city,status,attempts,next_attempt_at,note,raw,updated_at)
               VALUES('test',?,?,?,?,?,?,?,?,?,?,?,?,'','',?,0,?,?,?,?)""",
            (uuid.uuid4().hex[:16], "test", iso(now), iso(now), clean_name(name) or "Test Parent", clean_name(student),
             p, parent_phone, campus, campus, clean_grade(grade) or grade.strip()[:20], grade, Status.QUEUED.value, iso(now),
             f"test call by {who or 'super admin'}", "{}", iso(now)))
        self.store.kv_set(f"testnum_{lead_id}", c)
        self.store.add_audit(now, who, "test_call", f"test lead {lead_id}")
        # Own start button: a test goes out even while real calling is paused or stopped, and outside calling hours.
        row = self.store.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        ref = self._place(row, now)
        return lead_id, ("" if ref else "The call could not be placed. Check the Vapi connection and that the two phones differ from the calling number")

    def delete_test_data(self, now: datetime, who: str = "") -> int:
        n = 0
        for r in self.store.q("SELECT id FROM leads WHERE source='test'"):      # even one still 'calling': it is only a test
            self._erase_lead(r["id"])
            self.store.x("DELETE FROM kv WHERE k=?", (f"testnum_{r['id']}",))
            n += 1
        self.store.add_audit(now, who, "test_clear", f"{n} test enquiries")
        return n

    def _erase_lead(self, lead_id: int) -> None:
        """Remove a lead, its calls and alerts, and ask the calling provider to erase its recordings/transcripts too."""
        for c in self.store.q("SELECT provider_call_id FROM calls WHERE lead_id=?", (lead_id,)):
            if c["provider_call_id"]:
                self.dialer.delete_call_data(c["provider_call_id"])
        self.store.x("DELETE FROM calls WHERE lead_id=?", (lead_id,))
        self.store.x("DELETE FROM alerts WHERE lead_id=?", (lead_id,))
        self.store.x("DELETE FROM leads WHERE id=?", (lead_id,))

    def purge_old(self, now: datetime) -> dict:
        """Privacy retention. Recordings/transcripts are erased after privacy.recording_days, whole enquiries after
        privacy.lead_days (0 = keep). Never touches a call in progress."""
        pv = self.cfg.data.get("privacy", {})
        rec_d, lead_d = int(pv.get("recording_days", 0) or 0), int(pv.get("lead_days", 0) or 0)
        out = {"recordings": 0, "leads": 0}
        if rec_d > 0:
            cut = iso(now - timedelta(days=rec_d))
            for c in self.store.q("SELECT call_ref,provider_call_id FROM calls WHERE started_at<? AND "
                                  "(COALESCE(recording_url,'')<>'' OR COALESCE(transcript,'')<>'' OR COALESCE(summary,'')<>'')", (cut,)):
                if c["provider_call_id"]:
                    self.dialer.delete_call_data(c["provider_call_id"])
                self.store.x("UPDATE calls SET recording_url='', transcript='', summary='' WHERE call_ref=?", (c["call_ref"],))
                out["recordings"] += 1
        if lead_d > 0:
            cut = iso(now - timedelta(days=lead_d))
            for r in self.store.q("SELECT id FROM leads WHERE updated_at<? AND status<>?", (cut, Status.CALLING.value)):
                self._erase_lead(r["id"])
                out["leads"] += 1
        if out["recordings"] or out["leads"]:
            self.store.add_audit(now, "system", "purge", f"recordings {out['recordings']}, enquiries {out['leads']}")
        return out

    def resolve(self, lead_id: int, now: datetime, note: str = "", who: str = "") -> bool:
        """A person has dealt with this lead (called the parent back, etc.)."""
        r = self.store.one("SELECT status FROM leads WHERE id=?", (lead_id,))
        if not r:
            return False
        self.store.update_lead(lead_id, now, status=Status.RESOLVED.value, next_attempt_at=None,
                               note=(note or "resolved by a person")[:300])
        self.store.add_audit(now, who, "mark_done", f"lead {lead_id}")
        return True

    def report(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) n FROM leads WHERE source<>'test' GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
