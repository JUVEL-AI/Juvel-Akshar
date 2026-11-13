from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone

from .config import Config
from .dialers.base import Dialer, DialerError
from .models import CallEvent, CallPlan, EventKind, RawLead, Status
from .normalize import canonical_campus, clean_grade, clean_name, clean_phone
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

    # ---------- intake ----------
    def ingest(self, leads: list[RawLead], now: datetime) -> dict:
        out = {"new": 0, "skipped_existing": 0}
        for lead in sorted(leads, key=lambda l: l.created_at):
            if self.store.one("SELECT 1 FROM leads WHERE source=? AND key=?", (lead.source, lead.key)):
                out["skipped_existing"] += 1
                continue
            self._ingest_one(lead, now)
            out["new"] += 1
        return out

    def _ingest_one(self, lead: RawLead, now: datetime) -> int:
        phone = clean_phone(lead.phone)
        campus = canonical_campus(lead.campus)
        grade = clean_grade(lead.grade_text)
        status, note, next_at = Status.QUEUED, "", None

        if not phone:
            status, note = Status.INVALID_PHONE, f"could not read phone '{lead.phone}'"
        elif self.store.is_dnc(phone):
            status, note = Status.DO_NOT_CALL, "on do-not-call list"
        elif self._is_duplicate(phone, lead.created_at):
            status, note = Status.DUPLICATE, "same phone already enquired recently"
        elif campus not in self.cfg.campuses:
            status, note = Status.NEEDS_REVIEW, f"unknown campus '{lead.campus}'"
        elif now - lead.created_at > timedelta(hours=self.c["stale_after_hours"]):
            status, note = Status.STALE, "lead too old for automatic call"
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
        if status in (Status.INVALID_PHONE, Status.NEEDS_REVIEW):   # old leads (e.g. first sheet import) stay silent
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
        rows = self.store.q(
            "SELECT * FROM leads WHERE status IN (?,?) AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (Status.QUEUED.value, Status.RETRY.value, iso(now), limit))
        for r in rows:
            if not in_hours(now, self.c, self.cfg.tz):
                self.store.update_lead(r["id"], now, next_attempt_at=iso(next_open(now, self.c, self.cfg.tz)))
                continue
            ref = self._place(r, now)
            if ref:
                placed.append(ref)
        self.watchdog(now)
        return placed

    def build_plan(self, r) -> CallPlan:
        campus = self.cfg.campuses[r["campus"]]
        counselor = campus["counselors"][0]
        m = self.cfg.messages
        ctx = dict(school_name=m["school_name"], counselor_name=counselor["name"],
                   campus_label=campus["label"], student_name=r["student_name"], grade=r["grade"],
                   parent_name=r["name"] or "name not given")
        if r["student_name"] and r["grade"]:
            whisper = m["whisper_full"].format(**ctx)
        elif r["student_name"]:
            whisper = m["whisper_no_class"].format(**ctx)
        else:
            whisper = m["whisper_minimal"].format(**ctx)
        return CallPlan(
            call_ref=f"c-{uuid.uuid4().hex[:12]}", lead_id=r["id"], campus=r["campus"],
            parent_number=r["phone"], caller_id=campus["caller_id"],
            greeting_text=m["greeting"].format(**ctx), counselor_name=counselor["name"],
            counselor_numbers=list(counselor["numbers"]) + list(campus.get("backup_numbers", [])),
            counselor_ring_seconds=campus.get("counselor_ring_seconds", 25),
            whisper_text=whisper, fallback_text=m["fallback"].format(**ctx),
            max_call_seconds=self.c["max_call_seconds"], webhook_url=self.webhook_url)

    def _place(self, r, now: datetime) -> str | None:
        plan = self.build_plan(r)
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
        elif ev.kind == EventKind.TRANSFER_FAILED:
            self.store.update_lead(lead["id"], now, status=Status.ANSWERED_NOT_TRANSFERRED.value,
                                   last_outcome="TRANSFER_FAILED", next_attempt_at=None)
            counselors = self.cfg.campuses[lead["campus"]]["counselors"]
            self._alert(now, "transfer_failed", lead["id"],
                        f"CALL BACK NEEDED: parent answered but no counselor reached ({lead['campus']})",
                        self._lead_summary(lead) + "\nParent heard the fallback message and expects a call back. "
                        "Please call this parent as soon as possible.",
                        to=self.cfg.notifications.get("admin_emails", []))
        else:   # NO_ANSWER / BUSY / FAILED
            attempts = lead["attempts"]
            if attempts >= self.c["max_attempts"]:
                self.store.update_lead(lead["id"], now, status=Status.NO_RESPONSE.value,
                                       last_outcome=ev.kind.value, next_attempt_at=None)
                self._alert(now, "no_response", lead["id"],
                            f"Parent not reachable after {attempts} attempts",
                            self._lead_summary(lead) + "\nAll automatic attempts used. Please try manually.")
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
        for r in self.store.q("SELECT * FROM leads WHERE status=? AND alerted_not_called=0", (Status.QUEUED.value,)):
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
        if not to and lead_id:
            row = self.store.one("SELECT campus FROM leads WHERE id=?", (lead_id,))
            camp = self.cfg.campuses.get(row["campus"]) if row and row["campus"] else None
            to = (camp or {}).get("alert_emails") or None     # that campus's own admissions inbox
        ok = self.notifier.send(to or self.cfg.notifications.get("admin_emails", []), subject, body)
        if ok:
            self.store.x("UPDATE alerts SET sent=1 WHERE id=?", (aid,))

    def resolve(self, lead_id: int, now: datetime, note: str = "") -> bool:
        """A person has dealt with this lead (called the parent back, etc.)."""
        r = self.store.one("SELECT status FROM leads WHERE id=?", (lead_id,))
        if not r:
            return False
        self.store.update_lead(lead_id, now, status=Status.RESOLVED.value, next_attempt_at=None,
                               note=(note or "resolved by a person")[:300])
        return True

    def report(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) n FROM leads GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
