from __future__ import annotations
import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger("juvelcall.notify")


class Notifier:
    """Base: logs only."""
    def send(self, to: list[str], subject: str, body: str) -> bool:
        log.warning("ALERT to=%s subject=%s\n%s", to, subject, body)
        return True


class MemoryNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return True


class SmtpNotifier(Notifier):
    def __init__(self, host, port, user, password, sender):
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to, subject, body):
        try:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = self.sender, ", ".join(to), subject
            m.set_content(body)
            with smtplib.SMTP(self.host, self.port, timeout=15) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(m)
            return True
        except Exception as e:   # never let alerting crash the engine
            log.error("email failed: %s", e)
            return False


class WebhookNotifier(Notifier):
    """POSTs each alert as JSON to a webhook (e.g. a Make.com scenario that sends the email).
    If the webhook fails and a fallback notifier exists (SMTP), the fallback is tried."""
    def __init__(self, url: str, fallback: "Notifier | None" = None, timeout: int = 15):
        self.url, self.fallback, self.timeout = url, fallback, timeout

    def send(self, to, subject, body):
        try:
            import requests
            r = requests.post(self.url, json={"to": to, "to_text": ", ".join(to),
                                              "subject": subject, "body": body}, timeout=self.timeout)
            if r.status_code < 300:
                return True
            log.error("email webhook returned %s", r.status_code)
        except Exception as e:
            log.error("email webhook failed: %s", e)
        return self.fallback.send(to, subject, body) if self.fallback else False


def notifier_from_config(cfg) -> Notifier:
    n = cfg.notifications or {}
    smtp = n.get("smtp") or {}
    base: Notifier = Notifier()
    if smtp.get("host"):
        base = SmtpNotifier(smtp["host"], smtp.get("port", 587), smtp.get("user", ""),
                            smtp.get("password", ""), smtp.get("sender", "alerts@localhost"))
    if n.get("webhook_url"):
        return WebhookNotifier(n["webhook_url"], fallback=base if smtp.get("host") else None)
    return base
