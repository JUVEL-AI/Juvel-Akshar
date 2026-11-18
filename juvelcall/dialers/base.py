from __future__ import annotations
from abc import ABC, abstractmethod
from ..models import CallPlan


class DialerError(Exception):
    pass


class Dialer(ABC):
    """Adapter to a calling provider. Implement place_call for each provider (Volant AI, Airtel,
    Plivo, Exotel, Vapi/Retell...). It must start the call described by the plan and return the
    provider's call id. Results come back later as CallEvents (webhook)."""

    @abstractmethod
    def place_call(self, plan: CallPlan) -> str: ...

    def delete_call_data(self, provider_call_id: str) -> bool:
        """Ask the provider to erase its stored recording/transcript for one call. Best effort; default does nothing."""
        return False
