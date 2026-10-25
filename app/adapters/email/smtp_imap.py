"""Email over SMTP (send) and IMAP (receive).

Works with any mailbox provider (Gmail/Google Workspace with an app password,
Fastmail, Outlook/Microsoft 365, Zoho, self-hosted). Use a dedicated mailbox
for the recruiter agent: the poller reads unread mail and marks candidate
replies as read.
"""

import email
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, parsedate_to_datetime
from html import unescape

from app.adapters.email.base import IncomingEmail, OutgoingEmail
from app.config import Settings

log = logging.getLogger(__name__)


class SMTPEmailAdapter:
    name = "smtp"

    def __init__(self, settings: Settings):
        self.s = settings

    def build(self, out: OutgoingEmail) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.s.email_from_name, self.s.email_from))
        msg["To"] = out.to
        msg["Subject"] = out.subject
        msg["Message-ID"] = out.message_id
        if out.in_reply_to:
            msg["In-Reply-To"] = out.in_reply_to
        if out.references:
            msg["References"] = " ".join(out.references[-20:])
        msg.set_content(out.body)
        return msg

    def send(self, out: OutgoingEmail) -> str:
        msg = self.build(out)
        ctx = ssl.create_default_context()
        if self.s.smtp_port == 465:
            server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
        try:
            if self.s.smtp_port != 465 and self.s.smtp_use_tls:
                server.starttls(context=ctx)
            if self.s.smtp_username:
                server.login(self.s.smtp_username, self.s.smtp_password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return out.message_id


# ---------- receiving ----------

AUTO_SUBJECT_RE = re.compile(
    r"^(automatic reply|auto[- ]?reply|out of (the )?office|autoreply|away from|undeliverable|"
    r"delivery status notification|mail delivery failed)",
    re.IGNORECASE,
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))


def extract_body(msg: email.message.Message) -> str:
    plain, html = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain is not None:
        return plain.strip()
    return _html_to_text(html or "").strip()


def is_auto_reply(msg: email.message.Message) -> bool:
    auto = (msg.get("Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    if (msg.get("Precedence") or "").lower() in ("bulk", "auto_reply", "junk", "list"):
        return True
    return bool(AUTO_SUBJECT_RE.match(_decode(msg.get("Subject")).strip()))


def parse_incoming(raw: bytes, uid: str = "") -> IncomingEmail:
    msg = email.message_from_bytes(raw)
    refs = (msg.get("References") or "").split()
    try:
        received = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    except Exception:
        received = None
    return IncomingEmail(
        from_address=parseaddr(msg.get("From") or "")[1].lower(),
        subject=_decode(msg.get("Subject")),
        body=extract_body(msg),
        message_id=(msg.get("Message-ID") or "").strip(),
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=refs,
        received_at=received,
        uid=uid,
        auto_reply=is_auto_reply(msg),
    )


class IMAPInboxAdapter:
    name = "imap"
    LOOKBACK_DAYS = 7

    def __init__(self, settings: Settings):
        self.s = settings

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port, ssl_context=ssl.create_default_context(),
                                 timeout=30)
        conn.login(self.s.imap_username, self.s.imap_password)
        conn.select(self.s.imap_folder)
        return conn

    def fetch_new(self) -> list[IncomingEmail]:
        """Unread messages from the last few days. Does not mark them read (see mark_seen)."""
        since = (datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        conn = self._connect()
        out: list[IncomingEmail] = []
        try:
            typ, data = conn.uid("SEARCH", None, "UNSEEN", "SINCE", since)
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed: {typ}")
            for uid in (data[0] or b"").split():
                typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                out.append(parse_incoming(msg_data[0][1], uid=uid.decode()))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def mark_seen(self, uids: list[str]) -> None:
        if not uids:
            return
        conn = self._connect()
        try:
            conn.uid("STORE", ",".join(uids), "+FLAGS", "(\\Seen)")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
