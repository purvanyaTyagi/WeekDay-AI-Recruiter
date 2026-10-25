"""SMS via Twilio. Inbound SMS arrive on the webhook in app/routers/webhooks.py."""

from app.config import Settings


class TwilioSMSAdapter:
    name = "twilio"

    def __init__(self, settings: Settings):
        from twilio.rest import Client

        self.s = settings
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def send(self, to: str, body: str) -> str:
        msg = self.client.messages.create(to=to, from_=self.s.twilio_from_number, body=body)
        return msg.sid


def validate_signature(settings: Settings, url: str, params: dict, signature: str) -> bool:
    from twilio.request_validator import RequestValidator

    return RequestValidator(settings.twilio_auth_token).validate(url, params, signature)
