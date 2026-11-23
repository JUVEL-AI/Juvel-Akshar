from __future__ import annotations
import csv
import hashlib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from ..models import RawLead
from ..normalize import clean_grade


_MONTH_NAME = re.compile(r"^([A-Za-z]{3,9}) (\d{1,2}), (\d{4})(?:[ T]+(\d{1,2}):(\d{2}))?$")
_ISO_LOOSE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?$")
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def parse_time(s: str, tz: ZoneInfo, date_order: str = "mdy") -> datetime:
    """ISO times (Meta: 2026-09-25T06:53:57+05:30, also -07:00) and sheet-typed 'MM/DD/YYYY HH:MM:SS'.
    date_order says how a slash date reads ('mdy' like this school's sheet, or 'dmy'); a first number above 12
    is always a day. Raises ValueError when the text is not a time at all."""
    s = (s or "").strip()
    m = _MONTH_NAME.match(s)                        # 'November 17, 2026' (the school's website form; no time of day)
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                d = datetime.strptime(f"{m[1]} {m[2]}, {m[3]}", fmt)
                return d.replace(hour=int(m[4] or 0), minute=int(m[5] or 0), tzinfo=tz)
            except ValueError:
                continue
    m = _ISO_LOOSE.match(s)                         # '2026-10-05 9:05:03' (single-digit hour)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0), tzinfo=tz)
    m = _SLASH.match(s)
    if m:
        a, b, y, hh, mm, ss = int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0)
        day, mon = (a, b) if (date_order == "dmy" or a > 12) else (b, a)
        return datetime(y, mon, day, hh, mm, ss, tzinfo=tz)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def lead_from_row(row: dict, cols: dict, tz: ZoneInfo, source: str, row_ref: str, date_order: str = "mdy") -> RawLead:
    g = lambda k: (row.get(cols.get(k, ""), "") or "").strip()
    phone, created = g("phone"), g("created_at")
    if not phone and not g("name"):
        raise ValueError("empty row")          # blank/formatting rows in the sheet are not enquiries
    # stable identity: same person/time/phone = same lead, even if the sheet is re-read
    key = hashlib.sha1(f"{created}|{phone}|{g('name')}".encode()).hexdigest()[:16]
    try:
        when = parse_time(created, tz, date_order)
    except ValueError:
        # A row whose time cannot be read (hand-edited, shifted cells) must never be called as if it were new:
        # give it an old date so it is stored as STALE and nobody is rung.
        when = LONG_AGO
        row = {**row, "_time_unreadable": created}
    # Meta's form answers land in the wrong column now and then (a class in the 'school' column and a school in the
    # 'class' column), so when a backup class column is chosen, use whichever of the two reads as a class.
    grade = g("grade")
    if g("grade_alt") and not clean_grade(grade) and clean_grade(g("grade_alt")):
        grade = g("grade_alt")
    return RawLead(key=key, source=source, row_ref=row_ref, created_at=when,
                   name=g("name"), student_name=g("student_name"), phone=phone, campus=g("campus"),
                   grade_text=grade, city=g("city"), email=g("email"), raw=dict(row))


def read_csv(path: str, cols: dict, tz: ZoneInfo, source: str = "csv", date_order: str = "mdy") -> list[RawLead]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                out.append(lead_from_row(row, cols, tz, source, f"row {i}", date_order))
            except Exception:
                continue   # a broken row must not stop the rest
    return out
