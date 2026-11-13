"""Vercel entry point: the whole backend as one serverless FastAPI app. State lives in Supabase Postgres,
and Supabase pg_cron calls /admin/tick every minute (see supabase/cron.sql)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from juvelcall.app import build_engine, create_app          # noqa: E402
from juvelcall.config import Config                          # noqa: E402

_public = os.environ.get("PUBLIC_URL") or (
    "https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"] if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else None)

cfg = Config.load(os.path.join(ROOT, "config.yaml"))
app = create_app(build_engine(cfg, public_url=_public), run_loop=False)
