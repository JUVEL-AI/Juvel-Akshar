"""Replay a CSV of leads through the whole pipeline with a fake dialer and a fake clock."""
from __future__ import annotations
import random
from datetime import timedelta

from .dialers.mock import MockDialer
from .engine import Engine
from .models import CallEvent, EventKind
from .notify import MemoryNotifier
from .sources.csv_source import read_csv
from .store import Store


def run(cfg, csv_path: str, seed: int = 7, verbose: bool = True) -> dict:
    rnd = random.Random(seed)
    store, dialer, notif = Store(":memory:"), MockDialer(), MemoryNotifier()
    eng = Engine(cfg, store, dialer, notif)
    leads = read_csv(csv_path, cfg.sources["csv_columns"], cfg.tz, "sim")
    start = min(l.created_at for l in leads)
    end = max(l.created_at for l in leads) + timedelta(days=2)
    pending = sorted(leads, key=lambda l: l.created_at)
    now = start
    handled = 0
    while now <= end:
        while pending and pending[0].created_at <= now:
            eng.ingest([pending.pop(0)], now)
        placed = eng.tick(now)
        for ref in placed:                       # parent behaviour is random in the simulation
            r = rnd.random()
            if r < 0.55:
                eng.handle_event(CallEvent(ref, EventKind.ANSWERED), now)
                if rnd.random() < 0.92:
                    eng.handle_event(CallEvent(ref, EventKind.TRANSFERRED, transferred_to="counselor"), now + timedelta(seconds=40))
                else:
                    eng.handle_event(CallEvent(ref, EventKind.TRANSFER_FAILED), now + timedelta(seconds=60))
            else:
                eng.handle_event(CallEvent(ref, EventKind.NO_ANSWER), now + timedelta(seconds=30))
            handled += 1
        now += timedelta(minutes=1)
    rep = eng.report()
    if verbose:
        print(f"leads read: {len(leads)}   calls placed: {len(dialer.calls)}")
        for k, v in sorted(rep.items()):
            print(f"  {k:26s} {v}")
        print(f"  alerts raised: {len(notif.sent)}")
    return {"report": rep, "calls": len(dialer.calls), "alerts": len(notif.sent), "engine": eng}
