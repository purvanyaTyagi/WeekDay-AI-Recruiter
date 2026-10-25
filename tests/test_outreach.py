from datetime import timedelta

from sqlalchemy import select

from app.models import Candidate, Message, utcnow
from app.services import messaging, outreach_agent
from app.services.inbound import process_inbound
from tests.conftest import set_runtime

FIRST = {"action": "none", "subject": "Backend role at Our Company",
         "reply": "Hi Jane, I'm an AI assistant reaching out on behalf of Our Company about a Backend Engineer role.",
         "interview_start": None, "reason": "first outreach"}
REPLY = {"action": "none", "subject": None, "reply": "Great question! The role is remote-friendly.", "reason": "answer"}


def _start(db, c, job, channel="email"):
    return outreach_agent.start_outreach(db, c, job, channel)


def test_first_email_auto_sent_with_disclosure(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST)
    msg = _start(db, c, job)
    assert msg.status == "sent" and msg.ai_generated and msg.approval == "auto"
    assert len(fakes.email.outbox) == 1
    sent = fakes.email.outbox[0]
    assert sent.to == "jane@example.com" and sent.subject == "Backend role at Our Company"
    assert "AI Recruiting Assistant" in sent.body and "STOP" in sent.body
    # The prompt gets the job, the resume, and wraps candidate data as untrusted.
    prompt = fakes.llm.last_user_prompt()
    assert "Build Python APIs" in prompt and "Kafka" in prompt and "<resume>" in prompt
    assert "test/outreach" == fakes.llm.calls[-1]["model"]
    assert "never claim" in fakes.llm.last_system_prompt().lower()
    kinds = [a.kind for a in c.activities]
    assert "ai_draft" in kinds and "message_sent" in kinds and "outreach_started" in kinds


def test_first_sms_has_ai_prefix_and_stop(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue({**FIRST, "reply": "Hi Jane, a Backend Engineer role might suit you.", "subject": None})
    msg = _start(db, c, job, "sms")
    assert msg.status == "sent"
    to, body = fakes.sms.outbox[0]
    assert to == "+14155550123"
    assert body.startswith("[AI assistant for Our Company]") and body.endswith("Reply STOP to opt out.")


def test_global_review_mode_then_approve_with_edit(db, candidate_job, fakes):
    c, job = candidate_job
    set_runtime(db, send_mode="review")
    fakes.llm.queue(FIRST)
    msg = _start(db, c, job)
    assert msg.status == "pending_review" and not fakes.email.outbox
    messaging.approve_and_send(db, msg, body="Edited by recruiter. I'm an AI assistant.")
    db.commit()
    assert msg.status == "sent" and msg.approval == "human_approved" and msg.ai_generated
    assert fakes.email.outbox[0].body == "Edited by recruiter. I'm an AI assistant."


def test_per_candidate_review_override(db, candidate_job, fakes):
    c, job = candidate_job
    c.send_mode_override = "review"
    db.commit()
    fakes.llm.queue(FIRST)
    assert _start(db, c, job).status == "pending_review"


def test_per_candidate_auto_override_beats_global_review(db, candidate_job, fakes):
    c, job = candidate_job
    set_runtime(db, send_mode="review")
    c.send_mode_override = "auto"
    db.commit()
    fakes.llm.queue(FIRST)
    assert _start(db, c, job).status == "sent"


def test_reply_threads_email(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST, REPLY)
    first = _start(db, c, job)
    process_inbound(db, c, "email", "Is it remote?", thread_ids=[first.provider_message_id])
    assert len(fakes.email.outbox) == 2
    reply = fakes.email.outbox[1]
    assert reply.in_reply_to == first.provider_message_id
    assert reply.subject == "Re: Backend role at Our Company"
    assert "Is it remote?" in fakes.llm.last_user_prompt()


def test_daily_limit_holds_ai_drafts(db, candidate_job, fakes):
    c, job = candidate_job
    set_runtime(db, max_per_day=1)
    fakes.llm.queue(FIRST, REPLY)
    _start(db, c, job)
    process_inbound(db, c, "email", "Tell me more")
    held = db.scalars(select(Message).where(Message.status == "pending_review")).one()
    assert "limit" in held.hold_reason.lower()
    assert len(fakes.email.outbox) == 1


def test_quiet_hours_queue_then_flush(db, candidate_job, fakes):
    c, job = candidate_job
    local = utcnow().astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    start = (local - timedelta(hours=1)).strftime("%H:%M")
    end = (local + timedelta(hours=1)).strftime("%H:%M")
    set_runtime(db, quiet_hours_start=start, quiet_hours_end=end)
    fakes.llm.queue(FIRST)
    msg = _start(db, c, job)
    assert msg.status == "queued" and "Quiet hours" in msg.hold_reason
    assert not fakes.email.outbox
    assert messaging.flush_queued(db) == 0  # still quiet
    set_runtime(db, quiet_hours_start="00:00", quiet_hours_end="00:00")
    assert messaging.flush_queued(db) == 1
    db.refresh(msg)
    assert msg.status == "sent" and len(fakes.email.outbox) == 1


def test_stop_keyword_opts_out_without_calling_model(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST)
    _start(db, c, job)
    calls = len(fakes.llm.calls)
    process_inbound(db, c, "email", "Please stop contacting me.\n\nOn Mon, AI wrote:\n> hi")
    db.refresh(c)
    assert c.opted_out and c.agent_paused and c.needs_attention
    assert len(fakes.llm.calls) == calls  # no model call
    assert len(fakes.email.outbox) == 1
    assert any(a.kind == "opt_out" for a in c.activities)
    try:
        _start(db, c, job)
        raise AssertionError("should be blocked")
    except outreach_agent.AgentUnavailable:
        pass


def test_quoted_stop_text_is_ignored():
    from app.services.safety import is_opt_out

    assert is_opt_out("STOP")
    assert is_opt_out("I'm not interested, thanks")
    assert not is_opt_out("Yes, Tuesday works!\n\n> Reply STOP to opt out")
    assert not is_opt_out("I can't stop thinking about this role!")


def test_model_opt_out_action(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST, {"action": "opt_out", "reply": None, "reason": "Candidate accepted another offer"})
    _start(db, c, job)
    process_inbound(db, c, "email", "Thanks, but I just accepted another offer.")
    db.refresh(c)
    assert c.opted_out and c.needs_attention
    assert len(fakes.email.outbox) == 1


def test_paused_agent_does_not_reply(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST)
    _start(db, c, job)
    c.agent_paused = True
    db.commit()
    calls = len(fakes.llm.calls)
    process_inbound(db, c, "email", "What's the salary?")
    db.refresh(c)
    assert len(fakes.llm.calls) == calls and c.needs_attention
    assert db.scalar(select(Message).where(Message.direction == "inbound")).body == "What's the salary?"


def test_flag_action_holds_reply_for_review(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST, {"action": "flag_for_recruiter", "reply": "Let me check with the team.",
                            "reason": "Salary question"})
    _start(db, c, job)
    process_inbound(db, c, "email", "What's the salary range?")
    db.refresh(c)
    assert c.needs_attention and "Salary" in c.attention_reason
    held = db.scalars(select(Message).where(Message.status == "pending_review")).one()
    assert held.ai_generated


def test_invalid_json_is_retried(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue("Sure! Here's a message: hello", FIRST)
    assert _start(db, c, job).status == "sent"
    assert "not valid" in fakes.llm.calls[-1]["messages"][-1]["content"]


def test_invalid_json_twice_flags(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue("nope", '{"action": "finalize_interview", "reply": "ok"}')
    assert _start(db, c, job) is None
    db.refresh(c)
    assert c.needs_attention and not fakes.email.outbox
    assert any(a.kind == "ai_error" for a in c.activities)


def test_ui_shows_badges_and_review_queue(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST)
    r = auth_client.post(f"/candidates/{c.id}/outreach", data={"job_id": job.id, "channel": "email"},
                         follow_redirects=False)
    assert r.status_code == 303
    page = auth_client.get(f"/candidates/{c.id}").text
    assert "AI-generated" in page and "auto-sent" in page

    auth_client.post(f"/candidates/{c.id}/agent", data={"op": "mode_review"})
    fakes.llm.queue(REPLY)
    auth_client.post(f"/candidates/{c.id}/simulate-inbound", data={"body": "Is it remote?", "channel": "email"})
    review = auth_client.get("/review").text
    assert "Approve &amp; send" in review and "Great question" in review
    pending = db.scalars(select(Message).where(Message.status == "pending_review")).one()
    auth_client.post(f"/messages/{pending.id}/approve", data={"body": "Yes, remote is fine.", "subject": ""})
    db.expire_all()
    assert db.get(Message, pending.id).approval == "human_approved"
    page = auth_client.get(f"/candidates/{c.id}").text
    assert "human-approved" in page


def test_pause_button_and_settings(auth_client, db, candidate_job):
    c, _ = candidate_job
    auth_client.post(f"/candidates/{c.id}/agent", data={"op": "pause"})
    db.expire_all()
    assert db.get(Candidate, c.id).agent_paused
    r = auth_client.post("/settings", data={"send_mode": "review", "quiet_hours_start": "22:00",
                                             "quiet_hours_end": "07:30", "max_per_day": 5}, follow_redirects=False)
    assert r.status_code == 303
    r = auth_client.post("/settings", data={"send_mode": "x", "quiet_hours_start": "25:00",
                                             "quiet_hours_end": "07:30", "max_per_day": 5})
    assert r.status_code == 422
