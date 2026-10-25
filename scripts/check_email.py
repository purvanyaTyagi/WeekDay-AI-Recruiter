"""Check the SMTP (send) and IMAP (receive) settings in .env.

Usage:
  python -m scripts.check_email you@example.com
  python -m scripts.check_email you@example.com --timeout 300
  python -m scripts.check_email you@example.com --send-only
  python -m scripts.check_email --read-only

What it does:
  1. Logs in to SMTP and IMAP to check the credentials.
  2. Sends a test email with a unique code in the subject to the address given.
  3. Checks the IMAP inbox until a message with that code arrives.

If the address is the agent mailbox itself (IMAP_USERNAME), step 3 finds the
test email directly. If it's another address, reply to the test email from
that inbox and the script will find your reply.

Nothing is marked as read and nothing is written to the database.
"""

import argparse
import imaplib
import smtplib
import ssl
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid

from app.adapters.email.base import OutgoingEmail
from app.adapters.email.smtp_imap import IMAPInboxAdapter, SMTPEmailAdapter, parse_incoming
from app.config import get_settings

OK, FAIL = "[ok]  ", "[FAIL]"


def _hint(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, (smtplib.SMTPAuthenticationError,)) or "authentication" in text or "login" in text:
        return ("  Hint: check the username/password. Gmail and Outlook need an app password, "
                "not your normal password.")
    if isinstance(exc, (TimeoutError, ConnectionRefusedError)) or "timed out" in text or "refused" in text:
        return "  Hint: check the host and port, and that your network allows outgoing connections to it."
    if "ssl" in text or "certificate" in text or "wrong version" in text:
        return "  Hint: port 465 uses SSL, 587 uses STARTTLS. Make sure the port matches the server."
    return ""


def check_smtp_login(s) -> bool:
    print(f"SMTP  {s.smtp_host}:{s.smtp_port} as {s.smtp_username or '(no login)'}")
    try:
        ctx = ssl.create_default_context()
        if s.smtp_port == 465:
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        try:
            if s.smtp_port != 465 and s.smtp_use_tls:
                server.starttls(context=ctx)
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as exc:
        print(f"{FAIL} SMTP login: {type(exc).__name__}: {exc}\n{_hint(exc)}")
        return False
    print(f"{OK} SMTP login")
    return True


def check_imap_login(s) -> bool:
    print(f"IMAP  {s.imap_host}:{s.imap_port} as {s.imap_username}, folder {s.imap_folder}")
    try:
        conn = IMAPInboxAdapter(s)._connect()
        typ, data = conn.status(s.imap_folder, "(MESSAGES UNSEEN)")
        conn.logout()
    except Exception as exc:
        print(f"{FAIL} IMAP login: {type(exc).__name__}: {exc}\n{_hint(exc)}")
        return False
    detail = data[0].decode(errors="replace") if typ == "OK" and data and data[0] else ""
    print(f"{OK} IMAP login {detail}")
    return True


def send_test(s, to: str, code: str) -> str | None:
    subject = f"AI Recruiter email check {code}"
    body = (f"This is a test email from the AI Recruiter email check.\nCode: {code}\n\n"
            "If you're testing from another inbox, just reply to this email.")
    msg_id = make_msgid(domain=(s.email_from or "localhost@localhost").split("@")[-1])
    try:
        SMTPEmailAdapter(s).send(OutgoingEmail(to=to, subject=subject, body=body, message_id=msg_id))
    except Exception as exc:
        print(f"{FAIL} Send to {to}: {type(exc).__name__}: {exc}\n{_hint(exc)}")
        return None
    print(f"{OK} Sent \"{subject}\" from {s.email_from} to {to}")
    return msg_id


def find_message(s, code: str) -> tuple[str, str] | None:
    """Look for a message whose subject contains the code. Returns (from, subject)."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    conn = IMAPInboxAdapter(s)._connect()
    try:
        typ, data = conn.uid("SEARCH", None, "SINCE", since, "SUBJECT", f'"{code}"')
        if typ != "OK":
            return None
        for uid in (data[0] or b"").split():
            typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: don't mark as read
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple):
                em = parse_incoming(msg_data[0][1])
                if code in em.subject:
                    return em.from_address, em.subject
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return None


def wait_for_message(s, code: str, timeout: int, interval: int = 5) -> bool:
    print(f"      Checking {s.imap_username}'s inbox for code {code} (up to {timeout}s)...")
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = find_message(s, code)
        except Exception as exc:
            print(f"{FAIL} IMAP search: {type(exc).__name__}: {exc}\n{_hint(exc)}")
            return False
        if found:
            print(f"{OK} Received \"{found[1]}\" from {found[0]}")
            return True
        if time.monotonic() >= deadline:
            print(f"{FAIL} No message with code {code} arrived within {timeout}s.\n"
                  "  Hint: check spam, check IMAP_FOLDER, or reply to the test email if you sent it to another inbox.")
            return False
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check SMTP sending and IMAP reading using the settings in .env.")
    p.add_argument("email", nargs="?", help="address to send the test email to")
    p.add_argument("--timeout", type=int, default=120, help="seconds to wait for the message over IMAP (default 120)")
    p.add_argument("--send-only", action="store_true", help="only check SMTP and send the test email")
    p.add_argument("--read-only", action="store_true", help="only check the IMAP login")
    args = p.parse_args(argv)

    if args.send_only and args.read_only:
        p.error("choose at most one of --send-only and --read-only")
    if not args.read_only and not args.email:
        p.error("an email address is required (or use --read-only)")

    s = get_settings()
    do_send, do_read = not args.read_only, not args.send_only
    missing = []
    if do_send and not s.email_configured:
        missing.append("SMTP_HOST and EMAIL_FROM")
    if do_read and not s.imap_configured:
        missing.append("IMAP_HOST and IMAP_USERNAME")
    if missing:
        print(f"{FAIL} Not configured in .env: {', '.join(missing)}")
        return 2

    ok = True
    if do_send:
        ok &= check_smtp_login(s)
    if do_read:
        ok &= check_imap_login(s)
    if not ok:
        return 1
    if args.read_only:
        return 0

    code = uuid.uuid4().hex[:10]
    if not send_test(s, args.email, code):
        return 1
    if args.send_only:
        return 0

    if args.email.strip().lower() != s.imap_username.strip().lower():
        print(f"      {args.email} isn't the agent mailbox ({s.imap_username}). Reply to the test email from "
              f"{args.email} so the reply lands in the agent mailbox.")
    return 0 if wait_for_message(s, code, args.timeout) else 1


if __name__ == "__main__":
    sys.exit(main())
