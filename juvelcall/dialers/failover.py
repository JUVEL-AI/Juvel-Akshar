from __future__ import annotations
import logging
from .base import Dialer, DialerError
from ..models import CallPlan

log = logging.getLogger("juvelcall.dialer")


class FailoverDialer(Dialer):
    """Try the primary (Volant); if it errors, place the same call through the backup (Vapi)."""

    def __init__(self, primary: Dialer, backup: Dialer):
        self.primary, self.backup, self.failovers = primary, backup, 0

    def place_call(self, plan: CallPlan) -> str:
        try:
            return self.primary.place_call(plan)
        except DialerError as e:
            self.failovers += 1
            log.error("primary dialer failed (%s) - using backup", e)
            return "backup:" + self.backup.place_call(plan)
