from __future__ import annotations
from .base import Dialer, DialerError
from ..models import CallPlan


class MockDialer(Dialer):
    def __init__(self, fail_numbers: set[str] | None = None):
        self.calls: list[CallPlan] = []
        self.fail_numbers = fail_numbers or set()

    def place_call(self, plan: CallPlan) -> str:
        if plan.parent_number in self.fail_numbers:
            raise DialerError("simulated provider failure")
        self.calls.append(plan)
        return f"mock-{len(self.calls)}"
