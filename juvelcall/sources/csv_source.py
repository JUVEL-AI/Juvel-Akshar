from __future__ import annotations
import csv
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from ..models import RawLead


def parse_time(s: str, tz: ZoneInfo) -> datetime:
    s = (s or "").strip()
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def lead_from_row(row: dict, cols: dict, tz: ZoneInfo, source: str, row_ref: str) -> RawLead:
    g = lambda k: (row.get(cols.get(k, ""), "") or "").strip()
    phone, created = g("phone"), g("created_at")
    # stable identity: same person/time/phone = same lead, even if the sheet is re-read
    key = hashlib.sha1(f"{created}|{phone}|{g('name')}".encode()).hexdigest()[:16]
    return RawLead(key=key, source=source, row_ref=row_ref, created_at=parse_time(created, tz),
                   name=g("name"), student_name=g("student_name"), phone=phone, campus=g("campus"),
                   grade_text=g("grade"), city=g("city"), email=g("email"), raw=dict(row))


def read_csv(path: str, cols: dict, tz: ZoneInfo, source: str = "csv") -> list[RawLead]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                out.append(lead_from_row(row, cols, tz, source, f"row {i}"))
            except Exception:
                continue   # a broken row must not stop the rest
    return out
