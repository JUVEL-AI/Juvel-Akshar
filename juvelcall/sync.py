from __future__ import annotations
import logging
import requests

log = logging.getLogger("juvelcall.sync")


class VolantSync:
    """POSTs each finished lead/call to Volant (its /webhooks/make route triggers a workflow)."""
    def __init__(self, url: str):
        self.url = url

    def __call__(self, payload: dict) -> None:
        if not self.url:
            return
        try:
            requests.post(self.url, json=payload, timeout=8)
        except Exception as e:                # never break calling because the dashboard is down
            log.error("volant sync failed: %s", e)
