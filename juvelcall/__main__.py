"""python -m juvelcall serve | import-csv FILE | simulate FILE | check | test-call +91PARENT [campus]"""
import sys
from datetime import datetime, timezone

from .config import Config


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    cfg = Config.load("config.yaml")
    if cmd == "serve":
        import uvicorn
        from .app import build_engine, create_app
        import os
        eng = build_engine(cfg, public_url=os.environ.get("PUBLIC_URL"))
        uvicorn.run(create_app(eng, run_loop=True), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    elif cmd == "import-csv":
        from .app import build_engine
        from .sources.csv_source import read_csv
        eng = build_engine(cfg)
        leads = read_csv(argv[2], cfg.sources["csv_columns"], cfg.tz, "csv")
        print(eng.ingest(leads, datetime.now(timezone.utc)))
    elif cmd == "simulate":
        from .simulate import run
        run(cfg, argv[2])
    elif cmd == "check":
        from .check import run_check
        sys.exit(run_check(cfg))
    elif cmd == "test-call":
        from .check import test_call
        test_call(cfg, argv[2], argv[3] if len(argv) > 3 else "ecr")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
