import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
os.environ.setdefault('WEBHOOK_SECRET', 'test-secret')
os.environ.setdefault('VAPI_SERVER_SECRET', 'vapi-test')


@pytest.fixture(autouse=True)
def _clean_pg():
    """When tests run against Postgres (DATABASE_URL set), start every test from empty tables."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        import psycopg
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS leads, calls, alerts, dnc CASCADE")
    yield
