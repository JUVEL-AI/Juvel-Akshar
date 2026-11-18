from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Response
import hmac
from fastapi.middleware.cors import CORSMiddleware

from . import console, ops, sheets_admin

from .config import Config
from .dialers import dialer_from_config
from .engine import Engine
from .models import CallEvent, EventKind, RawLead
from .notify import notifier_from_config
from .sources.csv_source import parse_time
from .store import iso, Store


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_engine(cfg: Config, public_url: str | None = None) -> Engine:
    hook = f"{public_url}/webhooks/vapi" if public_url else None
    from .sync import VolantSync
    sync_url = (cfg.data.get("volant_sync") or {}).get("url", "")
    return Engine(cfg, Store(cfg.database), dialer_from_config(cfg), notifier_from_config(cfg), hook,
                  VolantSync(sync_url) if sync_url else None)


def create_app(engine: Engine, clock=now_utc, run_loop: bool = False, loop_seconds: int = 20) -> FastAPI:
    cfg = engine.cfg
    secret = cfg.security.get("webhook_secret", "")

    sheet_cfg = (cfg.sources or {}).get("sheet") or {}
    last_poll = {"t": 0.0}

    def poll_sheet() -> dict:
        from . import sheets_admin
        ops.apply_overrides(engine)
        return sheets_admin.poll_all(engine, clock())

    async def loop():
        import time
        while True:
            try:
                if sheets_admin.configured(engine) and time.time() - last_poll["t"] >= sheet_cfg.get("poll_seconds", 60):
                    last_poll["t"] = time.time()
                    await asyncio.to_thread(poll_sheet)
                engine.tick(clock())
            except Exception as e:
                import logging
                logging.getLogger("juvelcall").error("loop error: %s", e)
            await asyncio.sleep(loop_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(loop()) if run_loop else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Juvel admissions calling", lifespan=lifespan)
    origins = cfg.security.get("allowed_origins", [])
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                           allow_headers=["X-Webhook-Secret"])

    def auth(x_webhook_secret: str | None):
        if not secret or not x_webhook_secret or not hmac.compare_digest(x_webhook_secret.encode(), secret.encode()):   # fail closed; constant-time
            raise HTTPException(401, "bad secret")

    @app.get("/health")
    def health(x_webhook_secret: str | None = Header(default=None)):
        """Public uptime check shows nothing about enquiries; counts only with the secret."""
        out = {"ok": True, "time": clock().isoformat()}
        if secret and x_webhook_secret and hmac.compare_digest(x_webhook_secret.encode(), secret.encode()):
            out["leads"] = engine.report()
        return out

    @app.post("/webhooks/lead")
    def lead(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        """Called by Meta / Zoho Bigin / Make / the school's form the moment a registration arrives."""
        auth(x_webhook_secret)
        cols = cfg.sources.get("csv_columns", {})
        g = lambda k: str(payload.get(cols.get(k, k), payload.get(k, "")) or "")
        created = g("created_at")
        try:
            when = parse_time(created, cfg.tz, cfg.sources.get("date_order", "mdy")) if created else clock()
        except ValueError:
            when = clock()                      # a live webhook lead is new by definition; never drop it over a bad timestamp
        rl = RawLead(
            key=str(payload.get("id") or f"{created}|{g('phone')}|{g('name')}"), source="webhook", row_ref="webhook",
            created_at=when,
            name=g("name"), student_name=g("student_name"), phone=g("phone"), campus=g("campus"),
            grade_text=g("grade"), city=g("city"), email=g("email"), raw=payload)
        res = engine.ingest([rl], clock())
        engine.tick(clock())                       # first call goes out immediately when in hours
        return res

    @app.post("/webhooks/call-event")
    def call_event(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        """Called by the dialer provider with the result of a call."""
        auth(x_webhook_secret)
        try:
            kind = EventKind(str(payload["kind"]).upper())
        except (KeyError, ValueError):
            raise HTTPException(422, "kind must be one of " + ", ".join(e.value for e in EventKind))
        ev = CallEvent(payload.get("call_ref", ""), kind, payload.get("provider_call_id", ""),
                       payload.get("transferred_to", ""), payload.get("detail", ""))
        result = engine.handle_event(ev, clock())
        engine.tick(clock())
        return {"result": result}

    VAPI_END = {"customer-did-not-answer": EventKind.NO_ANSWER, "customer-busy": EventKind.BUSY,
                "voicemail": EventKind.NO_ANSWER, "assistant-forwarded-call": EventKind.TRANSFERRED,
                "assistant-forwarded-call-ended": EventKind.TRANSFERRED}

    @app.post("/webhooks/vapi")
    def vapi_hook(payload: dict, x_vapi_secret: str | None = Header(default=None)):
        """Vapi end-of-call report -> our call result. Only 'end-of-call-report' matters."""
        ops.apply_overrides(engine)               # the secret may have been saved from the dashboard
        vsecret = cfg.dialer.get("vapi", {}).get("server_secret", "")
        if not vsecret or not x_vapi_secret or not hmac.compare_digest(x_vapi_secret.encode(), vsecret.encode()):
            raise HTTPException(401, "bad secret")
        msg = payload.get("message", payload)
        if msg.get("type") != "end-of-call-report":
            return {"ignored": msg.get("type")}
        ref = (msg.get("call", {}).get("metadata") or {}).get("call_ref", "")
        reason = msg.get("endedReason", "")
        kind = VAPI_END.get(reason)
        if reason.startswith(("call.forwarding", "call.in-progress.error-transfer")):
            kind = EventKind.TRANSFER_FAILED            # the counselor was not reached: call the parent back later
        if kind is None:
            # parent answered but we never saw a forward = treat as needs-callback; hard errors = FAILED
            answered = not reason.startswith(("twilio-failed", "pipeline-error", "call.start.error", "phone-call-provider"))
            kind = EventKind.TRANSFER_FAILED if (answered and reason.endswith("ended-call")) else EventKind.FAILED
        art = msg.get("artifact") or {}
        try:                                         # keep recording, transcript and summary with the call, never block the result
            dur = msg.get("durationSeconds")
            engine.store.x(
                "UPDATE calls SET recording_url=?, transcript=?, summary=?, duration_seconds=?, cost=?, ended_reason=? WHERE call_ref=?",
                (str(art.get("recordingUrl") or msg.get("recordingUrl") or "")[:600],
                 str(art.get("transcript") or msg.get("transcript") or "")[:20000],
                 str((msg.get("analysis") or {}).get("summary") or msg.get("summary") or "")[:3000],
                 int(round(float(dur))) if dur is not None else None,
                 float(msg["cost"]) if msg.get("cost") is not None else None, reason[:120], ref))
        except Exception:
            pass
        res = engine.handle_event(CallEvent(ref, kind, msg.get("call", {}).get("id", ""), "", reason), clock())
        engine.tick(clock())
        return {"result": res, "mapped": kind.value}

    @app.post("/admin/tick")
    def tick(x_webhook_secret: str | None = Header(default=None)):
        """Heartbeat. Called every minute by Supabase pg_cron (or any scheduler)."""
        auth(x_webhook_secret)
        out: dict = {}
        every = sheet_cfg.get("poll_seconds", 60)
        if sheets_admin.configured(engine):
            last = engine.store.kv_get("sheet_polled_at")
            now_ts = clock().timestamp()
            if not last or now_ts - float(last) >= every - 5:
                engine.store.kv_set("sheet_polled_at", str(now_ts))
                try:
                    out["sheet"] = poll_sheet()
                except Exception as e:
                    out["sheet_error"] = str(e)[:200]
        out["placed"] = engine.tick(clock(), limit=10)
        try:                                           # reports and reminders must never stop calls
            out.update(ops.periodic(engine, clock()))
        except Exception as e:
            out["periodic_error"] = str(e)[:200]
        return out

    @app.get("/api/console")
    def api_console(day: str | None = None, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        return console.build(engine, clock(), day)

    @app.get("/api/control")
    def api_control_get(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        return engine.control_info()

    @app.post("/api/control")
    def api_control_set(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        try:
            return engine.set_control(str(payload.get("action", "")).lower(), clock(),
                                      str(payload.get("by") or "dashboard")[:40])
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/settings")
    def api_settings_get(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        ops.apply_overrides(engine)
        return ops.effective_settings(engine)

    @app.put("/api/settings")
    def api_settings_put(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        who = str(payload.pop("by", "dashboard"))[:40]
        try:
            return ops.save_settings(engine, payload, clock(), who)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/integrations")
    def api_integrations_get(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        ops.apply_overrides(engine)
        return ops.integrations_view(engine)

    @app.put("/api/integrations")
    def api_integrations_put(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        who = str(payload.pop("by", "dashboard"))[:40]
        try:
            return ops.save_integrations(engine, payload, clock(), who)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/integrations/test")
    def api_integrations_test(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        ops.apply_overrides(engine)
        return ops.test_vapi(engine)

    @app.get("/api/audit")
    def api_audit(limit: int = 40, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        rows = engine.store.q("SELECT created_at AS at,who,action,detail FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 200),))
        return [dict(r) for r in rows]

    @app.get("/api/search")
    def api_search(q: str = "", x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        return ops.search(engine, q)

    @app.get("/api/export")
    def api_export(day: str | None = None, by: str = "dashboard", x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        engine.store.add_audit(clock(), by[:40], "export", day or "today")
        return Response(ops.export_csv(engine, day, clock()), media_type="text/csv; charset=utf-8")

    @app.get("/api/lead/{lead_id}")
    def api_lead(lead_id: int, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        d = console.lead_detail(engine, lead_id)
        if not d:
            raise HTTPException(404, "no such lead")
        return d

    @app.post("/api/lead/{lead_id}/resolve")
    def api_resolve(lead_id: int, payload: dict | None = None, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        if not engine.resolve(lead_id, clock(), (payload or {}).get("note", ""), str((payload or {}).get("by", ""))[:40]):
            raise HTTPException(404, "no such lead")
        return {"ok": True}

    @app.post("/api/lead/{lead_id}/call-now")
    def api_call_now(lead_id: int, payload: dict | None = None, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        why = engine.call_now(lead_id, clock(), str((payload or {}).get("by", ""))[:40])
        if why:
            raise HTTPException(409, why)
        return {"ok": True}

    @app.delete("/api/lead/{lead_id}")
    def api_delete_lead(lead_id: int, by: str = "", x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        why = engine.delete_lead(lead_id, clock(), by[:40])
        if why:
            raise HTTPException(409, why)
        return {"ok": True}

    @app.get("/api/sources")
    def api_sources_get(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        ops.apply_overrides(engine)
        return sheets_admin.view(engine)

    @app.put("/api/sources")
    def api_sources_put(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        who = str(payload.pop("by", "dashboard"))[:40]
        try:
            return sheets_admin.save(engine, payload, clock(), who)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/sources/preview")
    def api_sources_preview(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        ops.apply_overrides(engine)
        try:
            return sheets_admin.preview(engine, str(payload.get("spreadsheet", "")), str(payload.get("worksheet", "")))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/admin/poll-sheet")
    def admin_poll(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        return poll_sheet()

    @app.get("/report")
    def report(x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        rows = engine.store.q(
            "SELECT id,created_at,name,student_name,phone,campus,grade,status,attempts,last_outcome,transferred_to,note "
            "FROM leads ORDER BY id DESC LIMIT 200")
        return {"summary": engine.report(), "leads": [dict(r) for r in rows]}

    return app
