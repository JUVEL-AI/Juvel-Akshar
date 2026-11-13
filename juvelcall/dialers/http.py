from __future__ import annotations
import requests
from .base import Dialer, DialerError
from ..models import CallPlan


class HttpDialer(Dialer):
    """Generic JSON-over-HTTP adapter. Posts the CallPlan to `url`; expects JSON with an id
    field. Adjust `build_payload` / `parse_response` once the provider's (e.g. Volant AI's)
    API is known - this is the only place that changes."""

    def __init__(self, url: str, auth_header: str = "Authorization", auth_value: str = "",
                 timeout_seconds: int = 10, **_):
        self.url, self.auth_header, self.auth_value, self.timeout = url, auth_header, auth_value, timeout_seconds

    def build_payload(self, p: CallPlan) -> dict:
        return {
            "reference": p.call_ref, "from": p.caller_id, "to": p.parent_number,
            "greeting_text": p.greeting_text,
            "transfer": {"numbers": p.counselor_numbers, "ring_seconds": p.counselor_ring_seconds,
                         "whisper_text": p.whisper_text},
            "fallback_text": p.fallback_text, "max_duration": p.max_call_seconds,
            "webhook_url": p.webhook_url,
        }

    def parse_response(self, data: dict) -> str:
        return str(data.get("id") or data.get("call_id") or data.get("uuid") or "")

    def place_call(self, plan: CallPlan) -> str:
        if not self.url:
            raise DialerError("dialer.http.url not configured")
        headers = {self.auth_header: self.auth_value} if self.auth_value else {}
        try:
            r = requests.post(self.url, json=self.build_payload(plan), headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return self.parse_response(r.json())
        except Exception as e:
            raise DialerError(str(e)) from e
