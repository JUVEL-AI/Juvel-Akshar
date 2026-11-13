from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def window(calling: dict, weekday: int):
    """(start, end) for a weekday, or None if no calls that day.
    Per-day `hours: {mon: ["08:30","14:30"], ...}` wins; otherwise days/start/end apply."""
    day = _DAYS[weekday]
    hours = calling.get("hours")
    if hours:
        w = hours.get(day)
        return (_hm(w[0]), _hm(w[1])) if w else None
    if day not in calling["days"]:
        return None
    return _hm(calling["start"]), _hm(calling["end"])


def in_hours(dt: datetime, calling: dict, tz: ZoneInfo) -> bool:
    l = dt.astimezone(tz)
    w = window(calling, l.weekday())
    return bool(w) and w[0] <= l.time() < w[1]


def next_open(dt: datetime, calling: dict, tz: ZoneInfo) -> datetime:
    """First moment >= dt that is inside calling hours."""
    l = dt.astimezone(tz)
    for i in range(15):
        day = (l + timedelta(days=i)).date()
        w = window(calling, day.weekday())
        if not w:
            continue
        s = datetime.combine(day, w[0], tzinfo=tz)
        e = datetime.combine(day, w[1], tzinfo=tz)
        if i == 0:
            if l < s:
                return s
            if l < e:
                return l
            continue
        return s
    return l
