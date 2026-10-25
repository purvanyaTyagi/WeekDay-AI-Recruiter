from email.message import EmailMessage

from sqlalchemy import select

from app.adapters.email.base import IncomingEmail, OutgoingEmail
from app.adapters.email.smtp_imap import IMAPInboxAdapter, SMTPEmailAdapter, parse_incoming
from app.config import get_settings
from app.models import Message
from app.services import outreach_agent
from app.services.inbound import poll_inbox

FIRST = {"action": "none", "subject": "Backend role", "reply": "Hi Jane, I'm an AI assistant from Our Company."}
REPLY = {"action": "none", "reply": "Happy to share more details."}


def _raw(subject="Re: Backend role", body="Sounds good", headers=None, html=False, frm="Jane <jane@example.com>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "agent@company.test", subject
    m["Message-ID"] = "<reply-1@mail.test>"
    for k, v in (headers or {}).items():
        m[k] = v
    if html:
        m.set_content("fallback")
        m.clear_content()
        m.set_content(f"<p>{body}</p><br><div>Thanks</div>", subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


# ---------- SMTP ----------


def test_smtp_send_sets_threading_headers(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = (host, port)

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    s = get_settings().model_copy(update={"smtp_host": "smtp.test", "smtp_port": 587, "smtp_username": "u",
                                         "smtp_password": "p", "email_from": "agent@company.test"})
    mid = SMTPEmailAdapter(s).send(OutgoingEmail(to="jane@example.com", subject="Re: Hi", body="Body",
                                                 message_id="<m2@company.test>", in_reply_to="<m1@company.test>",
                                                 references=["<m1@company.test>"]))
    assert mid == "<m2@company.test>"
    msg = sent["msg"]
    assert sent["tls"] and sent["login"] == "u" and sent["host"] == ("smtp.test", 587)
    assert msg["In-Reply-To"] == "<m1@company.test>" and msg["References"] == "<m1@company.test>"
    assert msg["Message-ID"] == "<m2@company.test>" and "agent@company.test" in msg["From"]


# ---------- parsing ----------


def test_parse_plain_reply():
    em = parse_incoming(_raw(headers={"In-Reply-To": "<m1@x>", "References": "<m0@x> <m1@x>"}), uid="7")
    assert em.from_address == "jane@example.com" and em.body == "Sounds good"
    assert em.in_reply_to == "<m1@x>" and em.references == ["<m0@x>", "<m1@x>"] and em.uid == "7"
    assert not em.auto_reply


def test_parse_html_only():
    em = parse_incoming(_raw(body="Tuesday &amp; 3pm", html=True))
    assert "Tuesday & 3pm" in em.body and "<p>" not in em.body


def test_auto_reply_detection():
    assert parse_incoming(_raw(headers={"Auto-Submitted": "auto-replied"})).auto_reply
    assert parse_incoming(_raw(subject="Out of Office: back Monday")).auto_reply
    assert parse_incoming(_raw(subject="=?utf-8?q?Automatic_reply=3A_away?=")).auto_reply


# ---------- IMAP ----------


def test_imap_fetch_and_mark_seen(monkeypatch):
    calls = []
    raw = _raw()

    class FakeIMAP:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            pass

        def login(self, u, p):
            calls.append(("login", u))

        def select(self, folder):
            calls.append(("select", folder))

        def uid(self, cmd, *args):
            calls.append((cmd, args))
            if cmd == "SEARCH":
                return "OK", [b"11 12"]
            if cmd == "FETCH":
                return "OK", [(b"11 (BODY[] {123}", raw), b")"]
            return "OK", [b""]

        def logout(self):
            pass

    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    s = get_settings().model_copy(update={"imap_host": "imap.test", "imap_username": "agent", "imap_password": "x"})
    a = IMAPInboxAdapter(s)
    emails = a.fetch_new()
    assert [e.uid for e in emails] == ["11", "12"]
    search = next(c for c in calls if c[0] == "SEARCH")
    assert "UNSEEN" in search[1] and "SINCE" in search[1]
    assert all("PEEK" in c[1][1] for c in calls if c[0] == "FETCH")  # fetch must not mark read
    a.mark_seen(["11"])
    assert ("STORE", ("11", "+FLAGS", "(\\Seen)")) in calls


# ---------- polling end-to-end ----------


def _inbox(fakes):
    from app.adapters import get_inbox_adapter

    return get_inbox_adapter()


def test_poll_inbox_threads_reply_and_answers(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST, REPLY)
    first = outreach_agent.start_outreach(db, c, job, "email")
    inbox = _inbox(fakes)
    inbox.inbox += [
        IncomingEmail(from_address="jane@example.com", subject="Re: Backend role", body="Tell me more",
                      message_id="<r1@x>", in_reply_to=first.provider_message_id, uid="1"),
        IncomingEmail(from_address="stranger@example.com", subject="Hello", body="spam", message_id="<s@x>", uid="2"),
        IncomingEmail(from_address="JANE@example.com", subject="Automatic reply: away", body="I'm away",
                      message_id="<ooo@x>", uid="3", auto_reply=True),
    ]
    assert poll_inbox(db) == 2
    assert inbox.seen == ["1", "3"]  # stranger left unread
    assert len(fakes.email.outbox) == 2  # first + one reply (auto-reply not answered)
    inbound = db.scalars(select(Message).where(Message.direction == "inbound").order_by(Message.id)).all()
    assert [m.engagement_id for m in inbound] == [first.engagement_id] * 2
    assert any(a.kind == "auto_reply_ignored" for a in c.activities)
    # Re-delivery of the same Message-ID is ignored.
    inbox.seen.clear()
    inbox.inbox = inbox.inbox[:1]
    poll_inbox(db)
    assert db.query(Message).filter(Message.direction == "inbound").count() == 2


# ---------- Twilio ----------


def _signed_post(client, monkeypatch, params, token="tw-token", sign=True):
    from twilio.request_validator import RequestValidator

    s = get_settings()
    monkeypatch.setattr(s, "twilio_auth_token", token)
    monkeypatch.setattr(s, "twilio_validate_signature", True)
    url = s.base_url + "/webhooks/twilio/sms"
    sig = RequestValidator(token).compute_signature(url, params) if sign else "bad"
    return client.post("/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": sig})


def test_twilio_webhook_rejects_bad_signature(client, monkeypatch, candidate_job):
    r = _signed_post(client, monkeypatch, {"From": "+14155550123", "Body": "hi", "MessageSid": "SM1"}, sign=False)
    assert r.status_code == 403


def test_twilio_webhook_records_and_replies(client, db, monkeypatch, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue({**FIRST, "subject": None}, REPLY)
    outreach_agent.start_outreach(db, c, job, "sms")
    r = _signed_post(client, monkeypatch, {"From": "+14155550123", "Body": "Interested!", "MessageSid": "SM1"})
    assert r.status_code == 200 and "<Response>" in r.text
    assert len(fakes.sms.outbox) == 2  # background reply ran
    db.expire_all()
    assert db.scalar(select(Message).where(Message.provider_message_id == "SM1")).body == "Interested!"


def test_twilio_stop_opts_out(client, db, monkeypatch, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue({**FIRST, "subject": None})
    outreach_agent.start_outreach(db, c, job, "sms")
    _signed_post(client, monkeypatch, {"From": "+14155550123", "Body": "STOP", "MessageSid": "SM2"})
    db.expire_all()
    from app.models import Candidate

    assert db.get(Candidate, c.id).opted_out
    assert len(fakes.sms.outbox) == 1


def test_twilio_adapter_send(monkeypatch):
    from app.adapters.sms import twilio as tw

    class FakeMessages:
        def create(self, to, from_, body):
            self.args = (to, from_, body)
            return type("M", (), {"sid": "SM123"})()

    class FakeClient:
        def __init__(self, sid, token):
            self.messages = FakeMessages()

    monkeypatch.setattr("twilio.rest.Client", FakeClient)
    s = get_settings().model_copy(update={"twilio_account_sid": "AC1", "twilio_auth_token": "t",
                                         "twilio_from_number": "+15550000000"})
    a = tw.TwilioSMSAdapter(s)
    assert a.send("+14155550123", "hello") == "SM123"
    assert a.client.messages.args == ("+14155550123", "+15550000000", "hello")
