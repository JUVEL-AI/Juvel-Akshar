from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import console

from .config import Config
from .dialers import dialer_from_config
from .engine import Engine
from .models import CallEvent, EventKind, RawLead
from .notify import notifier_from_config
from .sources.csv_source import parse_time
from .store import Store


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
        import os
        from .sources.sheets import read_sheet
        creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not (sheet_cfg.get("spreadsheet_id") and creds):
            return {"skipped": "sheet not configured"}
        leads = read_sheet(creds, sheet_cfg["spreadsheet_id"], sheet_cfg.get("worksheet", "Sheet1"),
                           cfg.sources["csv_columns"], cfg.tz, "meta_sheet")
        return engine.ingest(leads, clock())

    async def loop():
        import time
        while True:
            try:
                if sheet_cfg.get("spreadsheet_id") and time.time() - last_poll["t"] >= sheet_cfg.get("poll_seconds", 60):
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
        if not secret or x_webhook_secret != secret:   # fail closed: no secret configured = no access
            raise HTTPException(401, "bad secret")

    @app.get("/health")
    def health():
        return {"ok": True, "time": clock().isoformat(), "leads": engine.report()}

    @app.post("/webhooks/lead")
    def lead(payload: dict, x_webhook_secret: str | None = Header(default=None)):
        """Called by Meta / Zoho Bigin / Make / the school's form the moment a registration arrives."""
        auth(x_webhook_secret)
        cols = cfg.sources.get("csv_columns", {})
        g = lambda k: str(payload.get(cols.get(k, k), payload.get(k, "")) or "")
        created = g("created_at")
        rl = RawLead(
            key=str(payload.get("id") or f"{created}|{g('phone')}"), source="webhook", row_ref="webhook",
            created_at=parse_time(created, cfg.tz) if created else clock(),
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
        vsecret = cfg.dialer.get("vapi", {}).get("server_secret", "")
        if not vsecret or x_vapi_secret != vsecret:
            raise HTTPException(401, "bad secret")
        msg = payload.get("message", payload)
        if msg.get("type") != "end-of-call-report":
            return {"ignored": msg.get("type")}
        ref = (msg.get("call", {}).get("metadata") or {}).get("call_ref", "")
        reason = msg.get("endedReason", "")
        kind = VAPI_END.get(reason)
        if kind is None:
            # parent answered but we never saw a forward = treat as needs-callback; hard errors = FAILED
            answered = not reason.startswith(("twilio-failed", "pipeline-error", "call.start.error", "phone-call-provider"))
            kind = EventKind.TRANSFER_FAILED if (answered and reason.endswith("ended-call")) else EventKind.FAILED
        res = engine.handle_event(CallEvent(ref, kind, msg.get("call", {}).get("id", ""), "", reason), clock())
        engine.tick(clock())
        return {"result": res, "mapped": kind.value}

    @app.post("/admin/tick")
    def tick(x_webhook_secret: str | None = Header(default=None)):
        """Heartbeat. Called every minute by Supabase pg_cron (or any scheduler)."""
        auth(x_webhook_secret)
        out: dict = {}
        every = sheet_cfg.get("poll_seconds", 60)
        if sheet_cfg.get("spreadsheet_id"):
            last = engine.store.kv_get("sheet_polled_at")
            now_ts = clock().timestamp()
            if not last or now_ts - float(last) >= every - 5:
                engine.store.kv_set("sheet_polled_at", str(now_ts))
                try:
                    out["sheet"] = poll_sheet()
                except Exception as e:
                    out["sheet_error"] = str(e)[:200]
        out["placed"] = engine.tick(clock(), limit=10)
        return out

    @app.get("/api/console")
    def api_console(day: str | None = None, x_webhook_secret: str | None = Header(default=None)):
        auth(x_webhook_secret)
        return console.build(engine, clock(), day)

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
        if not engine.resolve(lead_id, clock(), (payload or {}).get("note", "")):
            raise HTTPException(404, "no such lead")
        return {"ok": True}

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
