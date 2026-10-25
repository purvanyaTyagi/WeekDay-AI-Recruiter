"""scripts/check_email.py with fake SMTP and IMAP servers."""

import pytest

from app.config import get_settings
from scripts import check_email


@pytest.fixture
def mailbox(monkeypatch):
    s = get_settings()
    for k, v in {"smtp_host": "smtp.test", "smtp_port": 587, "smtp_username": "agent@co.test",
                 "smtp_password": "pw", "email_from": "agent@co.test", "imap_host": "imap.test",
                 "imap_username": "agent@co.test", "imap_password": "pw"}.items():
        monkeypatch.setattr(s, k, v)
    box = {"sent": [], "fail_login": None}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            if box["fail_login"] == "smtp":
                import smtplib
                raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

        def send_message(self, msg):
            box["sent"].append(msg)

        def quit(self):
            pass

    class FakeIMAP:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            pass

        def login(self, u, p):
            if box["fail_login"] == "imap":
                raise Exception("[AUTHENTICATIONFAILED] Invalid credentials")

        def select(self, folder):
            return "OK", [b"1"]

        def status(self, folder, what):
            return "OK", [b'INBOX (MESSAGES 3 UNSEEN 1)']

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                code = args[-1].strip('"')
                hits = [str(i + 1).encode() for i, m in enumerate(box["sent"]) if code in m["Subject"]]
                return "OK", [b" ".join(hits)]
            if cmd == "FETCH":
                assert "PEEK" in args[1]  # must not mark anything as read
                return "OK", [(b"1 (BODY[] {1}", box["sent"][int(args[0]) - 1].as_bytes()), b")"]
            raise AssertionError(f"unexpected IMAP command {cmd}")

        def logout(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    return box


def test_send_and_read_back(mailbox, capsys):
    assert check_email.main(["agent@co.test", "--timeout", "1"]) == 0
    out = capsys.readouterr().out
    assert "[ok]   SMTP login" in out and "[ok]   IMAP login" in out
    assert "[ok]   Sent" in out and "[ok]   Received" in out
    assert mailbox["sent"][0]["To"] == "agent@co.test"


def test_message_never_arrives(mailbox, monkeypatch, capsys):
    monkeypatch.setattr(check_email, "find_message", lambda s, code: None)
    assert check_email.main(["someone@else.test", "--timeout", "0"]) == 1
    out = capsys.readouterr().out
    assert "Reply to the test email" in out and "No message with code" in out


def test_bad_smtp_password(mailbox, capsys):
    mailbox["fail_login"] = "smtp"
    assert check_email.main(["agent@co.test"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] SMTP login" in out and "app password" in out
    assert not mailbox["sent"]


def test_read_only_and_send_only(mailbox, capsys):
    assert check_email.main(["--read-only"]) == 0
    assert check_email.main(["x@y.test", "--send-only"]) == 0
    assert len(mailbox["sent"]) == 1


def test_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    assert check_email.main(["x@y.test"]) == 2
    assert "Not configured" in capsys.readouterr().out
