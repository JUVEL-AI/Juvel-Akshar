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
                 timeout_seconds: int = 15, transfer_style: str = "assistant",
                 immediate_transfer: bool = True, sip_verb: str = "dial",
                 voice_id: str = "", voice_provider: str = "11labs", voice_model: str = "eleven_turbo_v2_5", **_):
        self.api_key, self.phone_number_id = api_key, phone_number_id
        self.server_secret, self.timeout = server_secret, timeout_seconds
        self.transfer_style = transfer_style or "assistant"      # assistant | legacy
        self.immediate = bool(immediate_transfer)
        self.sip_verb = sip_verb or "dial"
        self.voice_id, self.voice_provider, self.voice_model = voice_id, voice_provider or "11labs", voice_model

    def _destinations(self, p: CallPlan) -> list:
        out = []
        for n in p.counselor_numbers:
            d = {"type": "number", "number": n, "description": f"{p.counselor_name}, admissions counselor"}
            if self.transfer_style == "legacy":
                d["transferPlan"] = {"mode": "warm-transfer-say-message", "message": p.whisper_text}
            else:
                d["sipVerb"] = self.sip_verb          # 'dial': Vapi places the call to the counselor itself (no SIP REFER needed from the carrier)
            out.append(d)
        return out

    def build_payload(self, p: CallPlan) -> dict:
        dests = self._destinations(p)
        system = ("You are a connector, not a conversational agent. Your only job is to call transferCall to the FIRST "
                  "destination immediately, before saying anything. If that transfer fails, try the next destination. "
                  "If all fail, say exactly: " + p.fallback_text + " then end the call. Never answer questions or chat.")
        tool = {"type": "transferCall", "destinations": dests}
        if self.transfer_style != "legacy":
            # Assistant-based warm transfer is the mode Vapi supports on BYO SIP trunks: a small assistant speaks the
            # whisper to the counselor, then the parent is connected. The greeting is the message spoken to the parent
            # right before the transfer starts.
            tool["messages"] = [{"type": "request-start", "content": p.greeting_text}]
            tool["transferPlan"] = {
                "mode": "warm-transfer-experimental",
                "transferAssistant": {
                    "firstMessage": p.whisper_text, "firstMessageMode": "assistant-speaks-first",
                    "maxDurationSeconds": 60, "silenceTimeoutSeconds": 15,
                    "model": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
                              "messages": [{"role": "system", "content":
                                  "You speak to the school counselor who just answered. You have already said the "
                                  "announcement. Do not say anything else. Then connect the call."}]},
                },
                "fallbackPlan": {"message": p.fallback_text, "endCallEnabled": True},
            }
        assistant = {
            "maxDurationSeconds": p.max_call_seconds,
            "model": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
                      "messages": [{"role": "system", "content": system}], "tools": [tool]},
        }
        if self.immediate and self.transfer_style != "legacy":
            assistant["firstMessageMode"] = "assistant-speaks-first-with-model-generated-message"
        else:
            assistant["firstMessage"] = p.greeting_text
        if self.voice_id:                      # a chosen voice (ElevenLabs by default); empty = Vapi's default voice
            v = {"provider": self.voice_provider, "voiceId": self.voice_id}
            if self.voice_provider == "11labs":
                v.update({"model": self.voice_model, "stability": 0.5, "similarityBoost": 0.75})
            assistant["voice"] = v
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
