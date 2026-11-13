from .base import Dialer, DialerError
from .mock import MockDialer
from .http import HttpDialer
from .vapi import VapiDialer
from .failover import FailoverDialer


def _one(kind: str, d: dict) -> Dialer:
    if kind == "vapi":
        return VapiDialer(**d["vapi"])
    if kind == "http":
        return HttpDialer(**d["http"])
    return MockDialer()


def dialer_from_config(cfg) -> Dialer:
    d = cfg.dialer or {"type": "mock"}
    primary = _one(d.get("type", "mock"), d)
    if d.get("backup"):                       # e.g. a second provider later (plivo/exotel via http)
        return FailoverDialer(primary, _one(d["backup"], d))
    return primary
