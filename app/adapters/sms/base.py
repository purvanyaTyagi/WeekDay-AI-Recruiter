from typing import Protocol, runtime_checkable


@runtime_checkable
class SMSAdapter(Protocol):
    name: str

    def send(self, to: str, body: str) -> str:
        """Send an SMS and return the provider message id (raise on failure)."""
        ...
