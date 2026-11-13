from __future__ import annotations
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml


def _env(v):
    """Replace '${NAME}' with the environment variable NAME (empty if unset), recursively."""
    if isinstance(v, str):
        m = re.fullmatch(r"\$\{(\w+)\}", v.strip())
        return os.environ.get(m.group(1), "") if m else v
    if isinstance(v, list):
        return [_env(x) for x in v]
    if isinstance(v, dict):
        return {k: _env(x) for k, x in v.items()}
    return v


class Config:
    def __init__(self, data: dict):
        self.data = data
        self.tz = ZoneInfo(data.get("timezone", "Asia/Kolkata"))
        self.calling = data["calling"]
        self.campuses = data["campuses"]
        self.messages = data["messages"]
        self.notifications = data.get("notifications", {})
        self.security = data.get("security", {})
        self.dialer = data.get("dialer", {"type": "mock"})
        self.sources = data.get("sources", {})
        self.database = data.get("database", "juvelcall.db")

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            return cls(_env(yaml.safe_load(f)))
