from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    message_id: str  # we generate it so replies can be threaded back
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)


@dataclass
class IncomingEmail:
    from_address: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    received_at: datetime | None = None
    uid: str = ""  # provider-side id used to mark the message as read
    auto_reply: bool = False  # out-of-office, bounce, etc.: never answered by the agent


@runtime_checkable
class EmailAdapter(Protocol):
    name: str

    def send(self, email: OutgoingEmail) -> str:
        """Send and return the provider message id (raise on failure)."""
        ...


@runtime_checkable
class InboxAdapter(Protocol):
    name: str

    def fetch_new(self) -> list[IncomingEmail]:
        """Return unread messages (without marking them read)."""
        ...

    def mark_seen(self, uids: list[str]) -> None:
        """Mark processed messages as read."""
        ...
