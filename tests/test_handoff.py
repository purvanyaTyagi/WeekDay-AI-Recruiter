"""Handoff flow: Outreach Agent action -> briefing -> interview record -> link sent."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import Engagement, Interview, Message, utcnow
from app.services import outreach_agent
from app.services.handoff import send_due_reminders
from app.services.inbound import process_inbound
from tests.conftest import set_runtime

IST = ZoneInfo("Asia/Kolkata")
FIRST = {"action": "none", "subject": "Backend role", "reply": "Hi Jane, I'm an AI assistant from Our Company."}
BRIEFING = ("The role needs strong Python, FastAPI and PostgreSQL skills. Probe the monolith-to-microservices "
            "migration on AWS for depth, and ask for concrete Kafka examples.")


def _future_ist(days=2, hour=15):
    d = (utcnow().astimezone(IST) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d


def _finalize(start_iso, reply="Wonderful, you're booked in!"):
    return {"action": "finalize_interview", "reply": reply, "interview_start": start_iso,
            "reason": "Candidate agreed to Tuesday 3pm"}


def _setup_conversation(db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(FIRST)
    outreach_agent.start_outreach(db, c, job, "email")
    return c, job


def test_full_handoff_flow(db, candidate_job, fakes):
    c, job = _setup_conversation(db, candidate_job, fakes)
    start = _future_ist()
    fakes.llm.queue(_finalize(start.isoformat()), BRIEFING)

    process_inbound(db, c, "email", "Yes! Let's do it on that day at 3pm.")

    # (b) briefing came from the briefing model with the JD and resume in the prompt
    briefing_call = fakes.llm.calls[-1]
    assert briefing_call["model"] == "test/outreach"  # BRIEFING_MODEL defaults to OUTREACH_MODEL
    prompt = briefing_call["messages"][-1]["content"]
    assert "Build Python APIs" in prompt and "Kafka" in prompt
    assert "protected attributes" in briefing_call["messages"][0]["content"]

    # (c) interview record
    iv = db.scalars(select(Interview)).one()
    assert iv.status == "scheduled" and iv.candidate_id == c.id and iv.job_id == job.id
    assert iv.scheduled_at == start.astimezone(ZoneInfo("UTC"))
    assert iv.briefing == BRIEFING
    assert len(iv.token) >= 40 and iv.channel == "email"
    assert db.get(Engagement, iv.engagement_id).status == "interview_scheduled"

    # (d) the candidate gets the link + time in one message, with the AI disclosure last
    assert len(fakes.email.outbox) == 2
    body = fakes.email.outbox[-1].body
    link = f"http://testserver/i/{iv.token}"
    assert body.startswith("Wonderful, you're booked in!")
    assert link in body and start.strftime("%H:%M") in body and "IST" in body
    assert "recorded" in body
    assert body.index(link) < body.index("AI Recruiting Assistant")
    msg = db.scalars(select(Message).order_by(Message.id.desc())).first()
    assert msg.ai_generated and msg.approval == "auto" and msg.status == "sent"

    kinds = [a.kind for a in sorted(c.activities, key=lambda a: a.id)]
    for k in ("handoff", "briefing", "interview_created", "interview_link"):
        assert k in kinds, k


def test_naive_time_uses_candidate_timezone(db, candidate_job, fakes):
    c, _ = _setup_conversation(db, candidate_job, fakes)
    start = _future_ist(days=3, hour=11)
    fakes.llm.queue(_finalize(start.replace(tzinfo=None).isoformat()), BRIEFING)
    process_inbound(db, c, "email", "Thursday 11am works")
    assert db.scalars(select(Interview)).one().scheduled_at == start.astimezone(ZoneInfo("UTC"))


def test_past_time_is_flagged_not_scheduled(db, candidate_job, fakes):
    c, _ = _setup_conversation(db, candidate_job, fakes)
    fakes.llm.queue(_finalize((utcnow() - timedelta(days=1)).isoformat()))
    process_inbound(db, c, "email", "Yesterday works")
    db.refresh(c)
    assert db.scalar(select(func.count()).select_from(Interview)) == 0
    assert c.needs_attention
    assert db.scalars(select(Message).where(Message.status == "pending_review")).one()


def test_briefing_failure_uses_fallback_and_flags(db, candidate_job, fakes):
    c, _ = _setup_conversation(db, candidate_job, fakes)
    from app.services.llm import LLMError

    def boom(_):
        raise LLMError("provider down")

    fakes.llm.queue(_finalize(_future_ist().isoformat()), boom)
    process_inbound(db, c, "email", "Sure, 3pm")
    iv = db.scalars(select(Interview)).one()
    db.refresh(c)
    assert "unavailable" in iv.briefing and "Python, FastAPI" in iv.briefing
    assert c.needs_attention
    assert len(fakes.email.outbox) == 2  # the link still goes out


def test_review_mode_holds_link_message(db, candidate_job, fakes):
    c, _ = _setup_conversation(db, candidate_job, fakes)
    set_runtime(db, send_mode="review")
    fakes.llm.queue(_finalize(_future_ist().isoformat()), BRIEFING)
    process_inbound(db, c, "email", "3pm is fine")
    iv = db.scalars(select(Interview)).one()
    held = db.scalars(select(Message).where(Message.status == "pending_review")).one()
    assert iv.token in held.body and len(fakes.email.outbox) == 1


def test_second_finalize_reschedules(db, candidate_job, fakes):
    c, _ = _setup_conversation(db, candidate_job, fakes)
    first, second = _future_ist(days=2), _future_ist(days=4)
    fakes.llm.queue(_finalize(first.isoformat()), BRIEFING, _finalize(second.isoformat(), "Moved, no problem."))
    process_inbound(db, c, "email", "3pm works")
    process_inbound(db, c, "email", "Actually can we move it two days later?")
    iv = db.scalars(select(Interview)).one()
    assert iv.scheduled_at == second.astimezone(ZoneInfo("UTC"))
    assert fakes.email.outbox[-1].body.startswith("Moved, no problem.")
    assert any(a.kind == "interview_rescheduled" for a in c.activities)


def test_manual_scheduling_route(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    fakes.llm.queue(BRIEFING)
    start = _future_ist(days=1, hour=10)
    r = auth_client.post(f"/candidates/{c.id}/interviews",
                         data={"job_id": job.id, "start_local": start.strftime("%Y-%m-%dT%H:%M"), "channel": "sms"},
                         follow_redirects=False)
    assert r.status_code == 303
    iv = db.scalars(select(Interview)).one()
    assert iv.scheduled_at == start.astimezone(ZoneInfo("UTC")) and iv.briefing == BRIEFING
    to, body = fakes.sms.outbox[-1]
    assert iv.token in body and to == c.phone
    msg = db.scalars(select(Message)).one()
    assert not msg.ai_generated and msg.approval == "none"
    page = auth_client.get(f"/candidates/{c.id}").text
    assert "Backend Engineer" in page and "scheduled" in page


def test_tokens_are_unique():
    from app.services.handoff import new_token

    tokens = {new_token() for _ in range(1000)}
    assert len(tokens) == 1000 and all(len(t) >= 40 for t in tokens)


def test_reminder_sent_once(db, candidate_job, fakes):
    c, job = candidate_job
    eng = Engagement(candidate_id=c.id, job_id=job.id, channel="email")
    db.add(eng)
    db.flush()
    iv = Interview(candidate_id=c.id, job_id=job.id, engagement_id=eng.id, token="t" * 43, channel="email",
                   scheduled_at=utcnow() + timedelta(minutes=20), created_at=utcnow() - timedelta(days=1))
    db.add(iv)
    db.commit()
    assert send_due_reminders(db) == 1
    assert send_due_reminders(db) == 0
    assert "Reminder" in fakes.email.outbox[-1].body and iv.token in fakes.email.outbox[-1].body


def _quiet_now(db):
    local = utcnow().astimezone(IST)
    set_runtime(db, quiet_hours_start=(local - timedelta(hours=1)).strftime("%H:%M"),
                quiet_hours_end=(local + timedelta(hours=3)).strftime("%H:%M"))


def test_link_waits_for_quiet_hours_when_interview_is_later(db, candidate_job, fakes):
    from app.services.handoff import schedule_manually

    c, job = candidate_job
    _quiet_now(db)
    fakes.llm.queue(BRIEFING)
    schedule_manually(db, c, job, utcnow() + timedelta(days=2), "email")
    db.commit()
    assert db.scalars(select(Message)).one().status == "queued"


def test_link_skips_quiet_hours_when_interview_is_soon(db, candidate_job, fakes):
    from app.services.handoff import schedule_manually

    c, job = candidate_job
    _quiet_now(db)
    fakes.llm.queue(BRIEFING)
    schedule_manually(db, c, job, utcnow() + timedelta(hours=2), "email")
    db.commit()
    assert db.scalars(select(Message)).one().status == "sent"
