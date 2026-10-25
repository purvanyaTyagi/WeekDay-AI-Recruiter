"""Provider selection. Real providers are used when credentials are set,
otherwise console adapters that log messages. Tests can override with set_adapters()."""

from app.adapters.email.base import EmailAdapter, InboxAdapter
from app.adapters.sms.base import SMSAdapter
from app.config import get_settings

_email: EmailAdapter | None = None
_inbox: InboxAdapter | None = None
_sms: SMSAdapter | None = None


def get_email_adapter() -> EmailAdapter:
    global _email
    if _email is None:
        s = get_settings()
        if s.email_configured:
            from app.adapters.email.smtp_imap import SMTPEmailAdapter

            _email = SMTPEmailAdapter(s)
        else:
            from app.adapters.email.console import ConsoleEmailAdapter

            _email = ConsoleEmailAdapter()
    return _email


def get_inbox_adapter() -> InboxAdapter:
    global _inbox
    if _inbox is None:
        s = get_settings()
        if s.imap_configured:
            from app.adapters.email.smtp_imap import IMAPInboxAdapter

            _inbox = IMAPInboxAdapter(s)
        else:
            from app.adapters.email.console import NullInboxAdapter

            _inbox = NullInboxAdapter()
    return _inbox


def get_sms_adapter() -> SMSAdapter:
    global _sms
    if _sms is None:
        s = get_settings()
        if s.twilio_configured:
            from app.adapters.sms.twilio import TwilioSMSAdapter

            _sms = TwilioSMSAdapter(s)
        else:
            from app.adapters.sms.console import ConsoleSMSAdapter

            _sms = ConsoleSMSAdapter()
    return _sms


def set_adapters(email=None, sms=None, inbox=None) -> None:
    global _email, _sms, _inbox
    _email, _sms, _inbox = email, sms, inbox
