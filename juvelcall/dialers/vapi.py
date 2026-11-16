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
                 timeout_seconds: int = 15, transfer_style: str = "blind",
                 immediate_transfer: bool = True, sip_verb: str = "dial",
                 voice_id: str = "", voice_provider: str = "11labs", voice_model: str = "eleven_turbo_v2_5", **_):
        self.api_key, self.phone_number_id = api_key, phone_number_id
        self.server_secret, self.timeout = server_secret, timeout_seconds
        self.transfer_style = transfer_style or "blind"      # blind (proven on the Vobiz trunk) | assistant (adds a whisper) | legacy
        self.immediate = bool(immediate_transfer)
        self.sip_verb = sip_verb or "dial"
        self.voice_id, self.voice_provider, self.voice_model = voice_id, voice_provider or "11labs", voice_model

    def _destinations(self, p: CallPlan, style: str) -> list:
        out = []
        for n in p.counselor_numbers:
            d = {"type": "number", "number": n, "description": f"{p.counselor_name}, admissions counselor"}
            if style == "legacy":
                d["transferPlan"] = {"mode": "warm-transfer-say-message", "message": p.whisper_text}
            elif style == "blind":
                # The setup tested live in Vapi: Vapi itself dials the counselor, no whisper.
                d["message"] = "Connecting you now."
                d["numberE164CheckEnabled"] = True
                d["transferPlan"] = {"mode": "blind-transfer", "sipVerb": self.sip_verb,
                                     "fallbackPlan": {"message": p.fallback_text, "endCallEnabled": True}}
            else:
                # transferPlan lives inside each destination (same shape as a saved Vapi tool)
                d["message"] = p.greeting_text if self.immediate else "Connecting you now."
                d["transferPlan"] = {
                    "mode": "warm-transfer-experimental", "sipVerb": self.sip_verb,
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
            out.append(d)
        return out

    def build_payload(self, p: CallPlan, style: str | None = None) -> dict:
        style = style or self.transfer_style
        dests = self._destinations(p, style)
        system = ("You are a connector, not a conversational agent. Your only job is to call transferCall to the FIRST "
                  "destination immediately, before saying anything. If that transfer fails, try the next destination. "
                  "If all fail, say exactly: " + p.fallback_text + " then end the call. Never answer questions or chat.")
        tool = {"type": "transferCall", "destinations": dests}
        if style == "blind":
            tool["messages"] = [{"type": "request-start", "content": p.greeting_text, "blocking": True}]
        elif style != "legacy":
            tool["messages"] = [{"type": "request-start", "blocking": True}]
        assistant = {
            "maxDurationSeconds": p.max_call_seconds,
            "model": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
                      "messages": [{"role": "system", "content": system}], "tools": [tool]},
        }
        if self.immediate and style != "legacy":
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

    def _post(self, payload: dict):
        return requests.post(self.URL, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)

    def place_call(self, plan: CallPlan) -> str:
        chain = {"blind": ["blind", "legacy"], "assistant": ["assistant", "blind", "legacy"], "legacy": ["legacy"]}.get(self.transfer_style, ["blind", "legacy"])
        first = ""
        try:
            for i, style in enumerate(chain):
                r = self._post(self.build_payload(plan, style))
                if r.status_code == 400 and i < len(chain) - 1:
                    first = first or r.text[:200]       # Vapi did not accept these settings: try the next safer way so the parent is still called
                    continue
                if r.status_code >= 400:
                    raise DialerError(f"vapi {r.status_code}: {r.text[:300]}" + (f" (first try: {first})" if first else ""))
                return str(r.json().get("id", ""))
            raise DialerError("vapi: no usable call settings")
        except DialerError:
            raise
        except Exception as e:
            raise DialerError(f"vapi: {e}") from e
