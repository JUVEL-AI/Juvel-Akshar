from __future__ import annotations
import json
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

    def _campus_states(self) -> dict:
        """Every campus switch in ONE query, kept for 5 seconds. (It was one query per lead, which on a big queue made the
        backend slower than Vercel's time limit.)"""
        import time
        c = getattr(self, "_cs_cache", None)
        if c and time.monotonic() - c[0] < 5:
            return c[1]
        keys = [f"control_{k}" for k in self.cfg.campuses]
        rows = self.store.q("SELECT k, v FROM kv WHERE k IN (%s)" % ",".join("?" * len(keys)), tuple(keys)) if keys else []
        got = {r["k"]: r["v"] for r in rows}
        out = {k: (got.get(f"control_{k}") if got.get(f"control_{k}") in self.CONTROL_LABEL else "running") for k in self.cfg.campuses}
        self._cs_cache = (time.monotonic(), out)
        return out

    def campus_state(self, campus: str) -> str:
        """One campus's own switch. Calls for a campus go out only if the master switch AND this switch are running."""
        return self._campus_states().get(campus, "running")

    def can_call(self, campus: str) -> bool:
        return self.control_state() == "running" and self.campus_state(campus) == "running"

    def control_info(self) -> dict:
        return {"state": self.control_state(), "changed_at": self.store.kv_get("control_changed_at") or "",
                "by": self.store.kv_get("control_by") or "",
                "campuses": {k: {"state": self.campus_state(k), "changed_at": self.store.kv_get(f"control_{k}_at") or "",
                                "by": self.store.kv_get(f"control_{k}_by") or ""} for k in self.cfg.campuses}}

    def set_campus_control(self, campus: str, action: str, now: datetime, who: str = "dashboard") -> dict:
        """Start / pause / stop for ONE campus. Same meaning as the master switch, but only that campus's enquiries are affected."""
        new = {"start": "running", "pause": "paused", "stop": "stopped"}.get(action)
        if new is None:
            raise ValueError("action must be start, pause or stop")
        if campus not in self.cfg.campuses:
            raise ValueError("unknown campus")
        old = self.campus_state(campus)
        parked = 0
        if new == "stopped":
            for r in self.store.q("SELECT id FROM leads WHERE source<>'test' AND campus=? AND status IN (?,?)",
                                  (campus, Status.QUEUED.value, Status.RETRY.value)):
                self.store.update_lead(r["id"], now, status=Status.NEEDS_REVIEW.value, next_attempt_at=None,
                                       note="calling was stopped for this campus; please follow up by hand")
                parked += 1
        if new == "running" and old != "running":
            self.store.x("UPDATE leads SET next_attempt_at=? WHERE campus=? AND status IN (?,?) AND next_attempt_at>?",
                         (iso(now), campus, Status.QUEUED.value, Status.RETRY.value, iso(now)))
        self.store.kv_set(f"control_{campus}", new)
        self._cs_cache = None
        self.store.kv_set(f"control_{campus}_at", iso(now))
        self.store.kv_set(f"control_{campus}_by", who)
        label = self.cfg.campuses[campus]["label"]
        self.store.add_audit(now, who, f"calling_{new}_{campus}", f"{label}: {old} -> {new}" + (f", parked {parked}" if parked else ""))
        if new != old:
            extra = f" {parked} waiting enquiries were moved to Needs review." if parked else ""
            self._alert(now, f"control_{campus}_{new}", None, f"Calling {self.CONTROL_LABEL[new].lower()} for {label} ({who})",
                        f"Automatic calling for the {label} campus is now {self.CONTROL_LABEL[new].upper()}, set by {who} at "
                        f"{now.astimezone(self.cfg.tz):%d %b %H:%M}.{extra}",
                        to=self.cfg.notifications.get("control_emails") or None)
        return self.control_info() | {"parked": parked}

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
        existing: set = set()                       # one query per source instead of one per row (sheets hold thousands of rows)
        for src in {l.source for l in leads}:
            existing |= {(src, r["key"]) for r in self.store.q("SELECT key FROM leads WHERE source=?", (src,))}
        bulk = []
        for lead in sorted(leads, key=lambda l: l.created_at):
            if (lead.source, lead.key) in existing:
                out["skipped_existing"] += 1
                continue
            existing.add((lead.source, lead.key))
            if baseline:                            # first read: everything already there is stored silently, in bulk
                bulk.append(self._insert_args(lead, now, Status.STALE, "already in the sheet before go-live", None))
            else:
                self._ingest_one(lead, now, baseline, since)
            out["new"] += 1
        self.store.insert_many(self.INSERT_LEAD, bulk)
        return out

    INSERT_LEAD = """INSERT INTO leads(source,key,row_ref,created_at,received_at,name,student_name,phone,phone_raw,
               campus,campus_raw,grade,grade_raw,email,city,status,attempts,next_attempt_at,note,raw,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)"""

    def _normalise(self, lead: RawLead):
        phone = clean_phone(lead.phone)
        campus = canonical_campus(lead.campus)
        grade = clean_grade(lead.grade_text)
        if not campus:
            grade = grade or clean_grade(lead.campus)
            raw = (lead.campus or "").strip().lower()
            gradelike = bool(clean_grade(lead.campus))
            open_answer = (not raw) or gradelike or any(w in raw for w in ("either", "any", "both", "no preference", "not sure", "none", "n/a"))
            campus = (self.cfg.sources.get("default_campus") or None) if open_answer else None
        return phone, campus, grade

    def _insert_args(self, lead: RawLead, now: datetime, status: Status, note: str, next_at) -> tuple:
        phone, campus, grade = self._normalise(lead)
        return (lead.source, lead.key, lead.row_ref, iso(lead.created_at), iso(now), clean_name(lead.name),
                clean_name(lead.student_name), phone, lead.phone, campus, lead.campus, grade, lead.grade_text,
                lead.email, lead.city, status.value, iso(next_at) if next_at else None, note,
                self.store.dump_raw(lead.raw), iso(now))

    def _ingest_one(self, lead: RawLead, now: datetime, baseline: bool = False, since: datetime | None = None) -> int:
        phone, campus, grade = self._normalise(lead)
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
        elif self.control_state() == "stopped" or self.campus_state(campus) == "stopped":
            status, note = Status.NEEDS_REVIEW, "arrived while calling was stopped; please follow up by hand"
        else:
            next_at = next_open(max(now, lead.created_at), self.c, self.cfg.tz)

        lead_id = self.store.insert(self.INSERT_LEAD, self._insert_args(lead, now, status, note, next_at))
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
    def _campus_holds(self, now: datetime) -> dict:
        """When each campus may place its next call. After a call that reached the counselor the counselor rests
        (counselor_rest_seconds, default 5 minutes after the call ended); after any other call there is a short gap."""
        rest = int(self.c.get("counselor_rest_seconds", 300) or 0)
        gap = int(self.c.get("min_gap_seconds", 30) or 0)
        holds: dict = {}
        for r in self.store.q("SELECT l.campus, c.outcome, c.started_at, c.ended_at FROM calls c JOIN leads l ON l.id=c.lead_id "
                              "WHERE l.source<>'test' AND c.started_at>? ORDER BY c.started_at DESC",
                              (iso(now - timedelta(seconds=max(rest, gap) + 1800)),)):
            if r["campus"] in holds:
                continue
            end = parse(r["ended_at"]) or parse(r["started_at"])
            holds[r["campus"]] = end + timedelta(seconds=rest if r["outcome"] == "TRANSFERRED" else gap)
        return holds

    def tick(self, now: datetime, limit: int = 50) -> list[str]:
        """Place every call that is due. Returns call_refs placed."""
        placed = []
        ops.apply_overrides(self)
        if self.control_state() != "running":
            self.watchdog(now)               # only closes stuck calls while paused/stopped
            return placed
        placed += self._advance_recall(now)
        rows = self.store.q(
            "SELECT * FROM leads WHERE source<>'test' AND status IN (?,?) AND next_attempt_at<=? ORDER BY allow_old, next_attempt_at, id DESC LIMIT ?",
            (Status.QUEUED.value, Status.RETRY.value, iso(now), limit))
        pace = int(self.c.get("backfill_per_minute", 3) or 3)       # older enquiries (start-date catch-up) go out a few a minute
        caught_up = self.store.one(                       # older-enquiry calls already placed in the last 60 s (every webhook also runs a tick)
            "SELECT COUNT(*) n FROM calls c JOIN leads l ON l.id=c.lead_id WHERE l.allow_old=1 AND c.started_at>?",
            (iso(now - timedelta(seconds=60)),))["n"]
        inflight = {x["campus"]: x["n"] for x in self.store.q(     # calls on the line per campus (one counselor takes one parent at a time)
            "SELECT campus, COUNT(*) n FROM leads WHERE source<>'test' AND status=? AND updated_at>? GROUP BY campus",
            (Status.CALLING.value, iso(now - timedelta(minutes=3))))}
        holds = self._campus_holds(now)
        for r in rows:
            made = parse(r["created_at"])
            if r["allow_old"] and caught_up >= pace:
                continue
            if holds.get(r["campus"]) and now < holds[r["campus"]] and self.store.kv_get(f"force_{r['id']}") is None:
                continue                 # the counselor is resting, or the last call on this campus has only just ended
            if self.c.get("one_call_per_campus", True) and inflight.get(r["campus"], 0) >= 1:
                continue                 # the next parent waits until this campus's current call has finished (new enquiries go before older ones)
            if (made and not r["allow_old"] and now - made > timedelta(hours=self.c["stale_after_hours"])
                    and self.store.kv_get(f"force_{r['id']}") is None):
                self.store.update_lead(r["id"], now, status=Status.STALE.value, next_attempt_at=None,
                                       note="waited too long (calling was paused or the system was down)")
                continue
            if self.campus_state(r["campus"]) != "running":
                continue                 # this campus is paused or stopped: its leads wait
            forced = self.store.kv_get(f"force_{r['id']}") is not None
            if not forced and not in_hours(now, self.c, self.cfg.tz):
                self.store.update_lead(r["id"], now, next_attempt_at=iso(next_open(now, self.c, self.cfg.tz)))
                continue
            if not self._on_duty(r["campus"]):
                continue                 # nobody on duty for this campus: lead waits, the watchdog emails after 10 min
            ref = self._place(r, now)
            if ref:
                placed.append(ref)
                inflight[r["campus"]] = inflight.get(r["campus"], 0) + 1
                holds[r["campus"]] = now + timedelta(seconds=int(self.c.get("min_gap_seconds", 30) or 0))
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

    def revive_range(self, source: str, d_from: datetime, d_to: datetime, now: datetime, deadline: float | None = None) -> dict:
        """One-off catch-up asked for by the school: enquiries dated d_from..d_to (inclusive) that were skipped as old are
        queued and called at the paced backfill rate. Bad phones, do-not-call numbers, unknown campuses and phones already
        enquired or called elsewhere are left alone."""
        end = d_to + timedelta(days=1)
        done = {"queued": 0, "skipped": 0}
        taken: set[str] = set()
        live = (Status.STALE.value, Status.DUPLICATE.value, Status.INVALID_PHONE.value, Status.DO_NOT_CALL.value, Status.NEEDS_REVIEW.value)
        rows = self.store.q("SELECT * FROM leads WHERE source=? AND status=? AND (note LIKE ? OR note LIKE ? OR note LIKE ?) ORDER BY created_at",
                            (source, Status.STALE.value, "already in the sheet%", "before the start date%", "lead too old%"))
        import time
        for r in rows:
            made = parse(r["created_at"])
            if not made or made < d_from or made >= end:
                continue
            if deadline is not None and time.monotonic() > deadline:
                done["partial"] = True                  # out of time for this request: the rest is done on the next minute
                break
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
            old = bool(lead["allow_old"])                  # older-enquiry catch-up: fewer tries, and no alert email per parent
            cap = int(self.c.get("backfill_max_attempts", 2) or 2) if old else self.c["max_attempts"]
            if attempts >= cap:
                self.store.update_lead(lead["id"], now, status=Status.NO_RESPONSE.value,
                                       last_outcome=ev.kind.value, next_attempt_at=None)
                if not old:
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
            if self.campus_state(r["campus"]) != "running":
                continue
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
        if not self.can_call(r["campus"]):
            return "Calling is paused or stopped (for the school or this campus). Press Start first"
        self.store.update_lead(lead_id, now, status=Status.QUEUED.value, attempts=0, next_attempt_at=iso(now),
                               note=f"call started by {who or 'an admin'}")
        self.store.kv_set(f"force_{lead_id}", iso(now))
        self.store.add_audit(now, who, "call_now", f"lead {lead_id}")
        self.tick(now)
        self.store.x("DELETE FROM kv WHERE k=?", (f"force_{lead_id}",))
        return ""

    # ---------- call-backs, one parent at a time ----------
    RECALL_OK = ("ANSWERED_NOT_TRANSFERRED", "NO_RESPONSE")

    def _recall_queue(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.store.kv_get("recall_queue") or "[]")]
        except Exception:
            return []

    def _save_recall(self, q: list[int]) -> None:
        self.store.kv_set("recall_queue", json.dumps(q))

    def recall(self, ids: list, now: datetime, who: str = "", campus: str = "") -> dict:
        """Counselor or admin: call these parents back, one at a time. The next call starts only when the previous one has ended."""
        if self.control_state() != "running":
            return {"queued": 0, "waiting": len(self._recall_queue()), "error": "Calling is paused or stopped. Press Start first"}
        q = self._recall_queue()
        added = 0
        for i in ids:
            r = self.store.one("SELECT * FROM leads WHERE id=?", (int(i),))
            if (not r or r["status"] not in self.RECALL_OK or not r["phone"] or not r["campus"]
                    or self.store.is_dnc(r["phone"]) or (campus and r["campus"] != campus) or r["id"] in q
                    or self.campus_state(r["campus"]) != "running"):
                continue
            q.append(r["id"])
            added += 1
        self._save_recall(q)
        self.store.add_audit(now, who, "recall", f"{added} parent(s) queued for call back")
        started = self._advance_recall(now)
        return {"queued": added, "waiting": len(self._recall_queue()), "started": len(started)}

    def recall_cancel(self, now: datetime, who: str = "") -> int:
        n = len(self._recall_queue())
        self._save_recall([])
        self.store.add_audit(now, who, "recall_cancel", f"{n} parent(s) taken off the call-back list")
        return n

    def _advance_recall(self, now: datetime) -> list[str]:
        """Start the next call-back if the previous one has finished, we are inside calling hours and the counselor is on duty."""
        q = self._recall_queue()
        if not q or self.control_state() != "running" or not in_hours(now, self.c, self.cfg.tz):
            return []
        active = self.store.kv_get("recall_active")
        if active:
            a = self.store.one("SELECT status, updated_at FROM leads WHERE id=?", (int(active),))
            t = parse(a["updated_at"]) if a else None
            if a and a["status"] in (Status.CALLING.value, Status.QUEUED.value) and t and now - t < timedelta(minutes=20):
                return []                                   # the previous call-back is still going
        while q:
            lid = q[0]
            r = self.store.one("SELECT * FROM leads WHERE id=?", (lid,))
            if not r or r["status"] not in self.RECALL_OK or not r["phone"] or self.store.is_dnc(r["phone"]):
                q.pop(0)
                continue
            if not self._on_duty(r["campus"]) or self.campus_state(r["campus"]) != "running":
                break                                       # nobody on duty, or this campus is paused: wait
            hold = self._campus_holds(now).get(r["campus"])
            if hold and now < hold:
                break                                       # the counselor is resting before the next call
            q.pop(0)
            self.store.update_lead(lid, now, status=Status.QUEUED.value, attempts=0, next_attempt_at=iso(now),
                                   note="call back started from the dashboard")
            ref = self._place(self.store.one("SELECT * FROM leads WHERE id=?", (lid,)), now)
            self._save_recall(q)
            if ref:
                self.store.kv_set("recall_active", str(lid))
                return [ref]
            return []
        self._save_recall(q)
        return []

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

    def apply_skips(self, source: str, flags: dict, now: datetime) -> int:
        """The school's sheet remarks say it already worked on these parents (for example 'Not qualified'): they are closed
        and never called. Only parents still waiting are touched; a parent already called or transferred is left alone."""
        keys, done = list(flags), 0
        waiting = (Status.QUEUED.value, Status.RETRY.value, Status.NEEDS_REVIEW.value, Status.STALE.value)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for r in self.store.q("SELECT id, key FROM leads WHERE source=? AND key IN (%s) AND status IN (?,?,?,?)" % ",".join("?" * len(chunk)),
                                  (source, *chunk, *waiting)):
                self.store.update_lead(r["id"], now, status=Status.RESOLVED.value, next_attempt_at=None,
                                       note=f"already worked on by the school: {flags[r['key']]}"[:300])
                done += 1
        return done

    PARKED_NOTES = ("calling was stopped%", "arrived while calling was stopped%")

    def queue_parked(self, now: datetime, who: str = "", campus: str = "") -> dict:
        """Put back the enquiries that a Stop moved to Needs review. One enquiry per phone number; numbers that already have
        an active or finished enquiry (transferred, no response, callback, done), are on the do-not-call list or have no valid
        campus are left alone. Enquiries under 46 hours old go out as new ones; older ones follow at the paced rate."""
        live = (Status.STALE.value, Status.DUPLICATE.value, Status.INVALID_PHONE.value, Status.DO_NOT_CALL.value, Status.NEEDS_REVIEW.value)
        camps = [campus] if campus else list(self.cfg.campuses)
        if not camps or any(c not in self.cfg.campuses for c in camps):
            return {"queued": 0, "error": "unknown campus"}
        where = (" FROM leads WHERE source<>'test' AND status=? AND phone IS NOT NULL AND phone<>'' AND campus IN (%s)"
                 " AND (note LIKE ? OR note LIKE ?)"
                 " AND phone NOT IN (SELECT phone FROM leads WHERE phone IS NOT NULL AND status NOT IN (%s))"
                 " AND phone NOT IN (SELECT phone FROM dnc)"
                 " AND id IN (SELECT MAX(id) FROM leads WHERE status=? GROUP BY phone)") % (",".join("?" * len(camps)), ",".join("?" * len(live)))
        args = (Status.NEEDS_REVIEW.value, *camps, *self.PARKED_NOTES, *live, Status.NEEDS_REVIEW.value)
        n = self.store.one("SELECT COUNT(*) n" + where, args)["n"]
        if n:
            cutoff = iso(now - timedelta(hours=46))
            self.store.x("UPDATE leads SET status=?, allow_old=CASE WHEN created_at>=? THEN 0 ELSE 1 END, attempts=0, next_attempt_at=?, "
                         "note='queued again by an admin', updated_at=? WHERE id IN (SELECT id" + where + ")",
                         (Status.QUEUED.value, cutoff, iso(now), iso(now), *args))
        self.store.add_audit(now, who, "queue_parked", f"{n} enquiries" + (f" ({campus})" if campus else ""))
        return {"queued": n}

    OUTCOME_CHOICES = {"interested_next": "Interested - next steps", "not_interested": "Not interested",
                       "needs_time": "Needs time to decide", "parent_callback": "Parent will call back",
                       "school_cannot_support": "School cannot support"}
    OUTCOMES = {**OUTCOME_CHOICES, "dnd": "Do not disturb",                   # older saves keep their wording
                "admitted": "Admitted", "not_admitted": "Not admitted", "interested": "Interested, follow up"}

    def set_outcome(self, lead_id: int, now: datetime, outcome: str, who: str = "", dnd: bool = False) -> str | None:
        """A counselor records how the conversation ended. Calling stops for this enquiry. With 'dnd' ticked the parent asked
        not to be called again: the number also goes on the do-not-call list. Returns an error message, or None when saved."""
        if outcome not in self.OUTCOME_CHOICES and not (dnd and outcome in ("", "dnd")):
            return "Unknown outcome"
        outcome = outcome or "dnd"
        r = self.store.one("SELECT status, phone FROM leads WHERE id=?", (lead_id,))
        if not r:
            return "No such enquiry"
        if r["status"] == Status.CALLING.value:
            return "A call to this parent is in progress"
        note = f"{self.OUTCOMES[outcome]}{' - asked not to be disturbed' if dnd and outcome != 'dnd' else ''} (marked by {who or 'a counselor'})"
        status = Status.RESOLVED.value
        if dnd:
            status = Status.DO_NOT_CALL.value
            if r["phone"]:
                self.store.add_dnc(r["phone"], now, f"asked not to be disturbed (marked by {who or 'a counselor'})")
        if r["status"] == Status.TRANSFERRED.value:
            status = Status.TRANSFERRED.value            # a parent who reached the counselor stays counted as connected; the outcome is kept beside it
        self.store.update_lead(lead_id, now, status=status, next_attempt_at=None, outcome=outcome, note=note[:300])
        self.store.add_audit(now, who, "outcome", f"lead {lead_id}: {outcome}{' + do not disturb' if dnd else ''}")
        return None

    def report(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) n FROM leads WHERE source<>'test' GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
