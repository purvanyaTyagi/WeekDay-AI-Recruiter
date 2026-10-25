import logging
import uuid

log = logging.getLogger("adapters.sms.console")


class ConsoleSMSAdapter:
    """Dev adapter: logs SMS instead of sending. Keeps an in-memory outbox."""

    name = "console"

    def __init__(self):
        self.outbox: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> str:
        self.outbox.append((to, body))
        sid = f"console-{uuid.uuid4().hex[:12]}"
        log.info("[console sms] to=%s sid=%s\n%s\n[end sms]", to, sid, body)
        return sid
