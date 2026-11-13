from __future__ import annotations
import requests
from .base import Dialer, DialerError
from ..models import CallPlan


class VapiDialer(Dialer):
    """Places the call through Vapi (the same engine Volant AI uses underneath).
    Inline assistant: says the greeting, then uses Vapi's transferCall with a warm transfer so the
    counselor hears the whisper before the parent is connected.
    NOTE: Vapi uses an LLM to trigger the transfer tool; the prompt forces it to transfer at once.
    UNTESTED against live Vapi - verify in the test call (caller ID, transfer, whisper)."""

    URL = "https://api.vapi.ai/call"

    def __init__(self, api_key: str, phone_number_id: str, server_secret: str = "",
                 timeout_seconds: int = 15, **_):
        self.api_key, self.phone_number_id = api_key, phone_number_id
        self.server_secret, self.timeout = server_secret, timeout_seconds

    def build_payload(self, p: CallPlan) -> dict:
        dests = [{"type": "number", "number": n,
                  "transferPlan": {"mode": "warm-transfer-say-message", "message": p.whisper_text}}
                 for n in p.counselor_numbers]
        assistant = {
            "firstMessage": p.greeting_text,
            "maxDurationSeconds": p.max_call_seconds,
            "model": {
                "provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
                "messages": [{"role": "system", "content":
                    "You are a connector, not a conversational agent. Right after the greeting, call "
                    "transferCall to the FIRST destination. If that transfer fails, try the next "
                    "destination. If all fail, say exactly: " + p.fallback_text + " then end the call. "
                    "Never answer questions or chat."}],
                "tools": [{"type": "transferCall", "destinations": dests}],
            },
        }
        if p.webhook_url:
            assistant["server"] = {"url": p.webhook_url, "secret": self.server_secret} if self.server_secret \
                else {"url": p.webhook_url}
        return {"phoneNumberId": self.phone_number_id, "customer": {"number": p.parent_number},
                "metadata": {"call_ref": p.call_ref, "lead_id": p.lead_id}, "assistant": assistant}

    def place_call(self, plan: CallPlan) -> str:
        try:
            r = requests.post(self.URL, json=self.build_payload(plan),
                              headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
            if r.status_code >= 400:
                raise DialerError(f"vapi {r.status_code}: {r.text[:300]}")
            return str(r.json().get("id", ""))
        except DialerError:
            raise
        except Exception as e:
            raise DialerError(f"vapi: {e}") from e
