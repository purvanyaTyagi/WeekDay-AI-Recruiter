import logging

from app.adapters.email.base import IncomingEmail, OutgoingEmail

log = logging.getLogger("adapters.email.console")


class ConsoleEmailAdapter:
    """Dev adapter: logs emails instead of sending them. Keeps an in-memory outbox."""

    name = "console"

    def __init__(self):
        self.outbox: list[OutgoingEmail] = []

    def send(self, email: OutgoingEmail) -> str:
        self.outbox.append(email)
        log.info(
            "[console email] to=%s subject=%r message_id=%s\n%s\n[end email]",
            email.to,
            email.subject,
            email.message_id,
            email.body,
        )
        return email.message_id


class NullInboxAdapter:
    name = "none"

    def __init__(self):
        self.inbox: list[IncomingEmail] = []  # tests can drop messages here
        self.seen: list[str] = []

    def fetch_new(self) -> list[IncomingEmail]:
        return [m for m in self.inbox if m.uid not in self.seen]

    def mark_seen(self, uids: list[str]) -> None:
        self.seen.extend(uids)
