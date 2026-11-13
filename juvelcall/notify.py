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


def notifier_from_config(cfg) -> Notifier:
    smtp = (cfg.notifications or {}).get("smtp") or {}
    if smtp.get("host"):
        return SmtpNotifier(smtp["host"], smtp.get("port", 587), smtp.get("user", ""),
                            smtp.get("password", ""), smtp.get("sender", "alerts@localhost"))
    return Notifier()
