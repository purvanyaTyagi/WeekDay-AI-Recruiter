"""Handoff from the Outreach Agent to the Interview Agent.

finalize_interview:
  a. loads profile, resume text and job description
  b. asks BRIEFING_MODEL for a briefing paragraph
  c. creates the Interview record with an unguessable token
  d. sends the candidate the link and time on the engagement's channel
"""

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Candidate,
    Channel,
    Engagement,
    EngagementStatus,
    Interview,
    InterviewStatus,
    Message,
    utcnow,
)
from app.services import messaging, safety
from app.services.activity import log_activity
from app.services.llm import LLMError, get_llm, load_prompt
from app.templating import localdt

log = logging.getLogger(__name__)

RESUME_CHAR_LIMIT = 12000


def interview_url(interview: Interview) -> str:
    return f"{get_settings().base_url.rstrip('/')}/i/{interview.token}"


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


# ---------- briefing ----------


def generate_briefing(db: Session, candidate: Candidate, engagement: Engagement) -> tuple[str, bool]:
    """Return (briefing, ok). Falls back to a job-only briefing if the model fails."""
    s = get_settings()
    job = engagement.job
    resume = candidate.resume_text[:RESUME_CHAR_LIMIT]
    if not resume:
        resume = "(no resume text available)"
    elif candidate.resume_parse_status in ("partial", "failed"):
        resume += "\n(Note: the resume could only be partially read.)"
    user = f"""<job>
Title: {job.title}
Required skills: {job.required_skills or "(not specified)"}
Description:
{job.description}
</job>

<candidate_profile>
Recruiter notes: {candidate.summary_notes or "(none)"}
</candidate_profile>

<resume>
{resume}
</resume>

Write the briefing paragraph now."""
    messages = [{"role": "system", "content": load_prompt("briefing")}, {"role": "user", "content": user}]
    try:
        text = get_llm().complete(s.briefing_model, messages, temperature=0.3).strip()
        log_activity(db, candidate.id, "briefing", "Briefing paragraph generated for the Interview Agent",
                     actor="ai", detail={"model": s.briefing_model, "chars": len(text)})
        return text, True
    except LLMError as exc:
        log_activity(db, candidate.id, "ai_error", f"Briefing generation failed: {str(exc)[:300]}", actor="ai")
        fallback = (
            f"(Automatic briefing unavailable; written from the job description only.) "
            f"The role is {job.title}. Focus on the required skills"
            f"{': ' + job.required_skills if job.required_skills else ''}, ask for concrete examples from the "
            f"candidate's recent work, and probe depth in the areas the job description emphasises."
        )
        return fallback, False


# ---------- link message ----------


def _link_text(interview: Interview, candidate: Candidate) -> str:
    s = get_settings()
    when = localdt(interview.scheduled_at, candidate.timezone, "%A %d %B %Y, %H:%M %Z")
    url = interview_url(interview)
    if interview.channel == Channel.sms.value:
        return f"Interview: {when}. Link: {url} (AI text interview, ~{s.interview_max_minutes} min, recorded for the hiring team)"
    return (
        f"Your interview is scheduled for {when}.\n"
        f"Join here: {url}\n\n"
        f"It's a text chat with our AI interviewer and takes about {s.interview_max_minutes} minutes. "
        f"The conversation is recorded and reviewed by the hiring team. "
        f"The link opens {s.interview_grace_before_minutes} minutes before the start time; no login is needed."
    )


def send_interview_link(
    db: Session,
    interview: Interview,
    candidate: Candidate,
    intro: str | None,
    kind: str,
    subject: str = "",
) -> Message:
    from app.services.outreach_agent import add_disclosure

    engagement = db.get(Engagement, interview.engagement_id) if interview.engagement_id else None
    body = f"{intro.strip()}\n\n{_link_text(interview, candidate)}" if intro else _link_text(interview, candidate)
    if kind == "ai" and engagement is not None:
        body = add_disclosure(db, engagement, body)
    # Quiet hours normally hold messages until morning, but the link must arrive
    # well before the interview starts.
    quiet_until = safety.quiet_hours_until(db, candidate)
    urgent = bool(quiet_until and quiet_until > interview.scheduled_at - timedelta(hours=1))
    msg = messaging.submit_outbound(db, candidate, engagement, interview.channel, body, kind, subject=subject,
                                    skip_quiet_hours=urgent)
    log_activity(db, candidate.id, "interview_link", f"Interview link {msg.status.replace('_', ' ')} "
                 f"({interview.channel})", actor="ai" if kind == "ai" else "system",
                 detail={"interview_id": interview.id, "message_id": msg.id})
    return msg


# ---------- main entry ----------


def active_interview(db: Session, candidate_id: int, job_id: int) -> Interview | None:
    return db.scalar(
        select(Interview).where(
            Interview.candidate_id == candidate_id,
            Interview.job_id == job_id,
            Interview.status == InterviewStatus.scheduled.value,
        )
    )


def finalize_interview(
    db: Session,
    candidate: Candidate,
    engagement: Engagement,
    start: datetime,
    confirmation: str | None,
    source: str,  # "agent" | "recruiter"
) -> Message | None:
    """Create (or move) the interview, write the briefing, and send the link."""
    existing = active_interview(db, candidate.id, engagement.job_id)
    if existing:
        # The candidate agreed a new time for an interview that is already set up.
        return reschedule_interview(db, existing, start, actor=source, intro=confirmation)

    log_activity(db, candidate.id, "handoff",
                 f"Handoff to Interview Agent: interview agreed for {localdt(start, candidate.timezone)}",
                 actor="ai" if source == "agent" else "recruiter")
    briefing, ok = generate_briefing(db, candidate, engagement)
    interview = Interview(
        candidate=candidate,
        job_id=engagement.job_id,
        engagement_id=engagement.id,
        token=new_token(),
        scheduled_at=start,
        status=InterviewStatus.scheduled.value,
        briefing=briefing,
        channel=engagement.channel,
    )
    db.add(interview)
    engagement.status = EngagementStatus.interview_scheduled.value
    db.flush()
    log_activity(db, candidate.id, "interview_created",
                 f"Interview scheduled for {localdt(start, candidate.timezone)}",
                 actor="ai" if source == "agent" else "recruiter", detail={"interview_id": interview.id})
    if not ok:
        candidate.needs_attention = True
        candidate.attention_reason = "The interview briefing could not be generated; a fallback was used."

    kind = "ai" if source == "agent" else "system"
    subject = f"Your interview for {engagement.job.title}" if source == "recruiter" else ""
    return send_interview_link(db, interview, candidate, confirmation, kind, subject=subject)


def schedule_manually(db: Session, candidate: Candidate, job, start: datetime, channel: str) -> Interview:
    from app.services.outreach_agent import get_or_create_engagement

    engagement = get_or_create_engagement(db, candidate, job, channel)
    finalize_interview(db, candidate, engagement, start, confirmation=None, source="recruiter")
    db.flush()
    return active_interview(db, candidate.id, job.id)


def reschedule_interview(
    db: Session, interview: Interview, start: datetime, actor: str = "recruiter", intro: str | None = None
) -> Message:
    if interview.status not in (InterviewStatus.scheduled.value, InterviewStatus.no_show.value,
                                InterviewStatus.cancelled.value):
        raise ValueError("Only scheduled, no-show or cancelled interviews can be rescheduled.")
    candidate = interview.candidate
    old = interview.scheduled_at
    interview.scheduled_at = start
    interview.status = InterviewStatus.scheduled.value
    interview.reminder_sent_at = None
    interview.end_reason = ""
    log_activity(db, candidate.id, "interview_rescheduled",
                 f"Interview moved from {localdt(old, candidate.timezone)} to {localdt(start, candidate.timezone)}",
                 actor="ai" if actor == "agent" else "recruiter", detail={"interview_id": interview.id})
    if intro is None:
        intro = "Your interview has been rescheduled."
        kind = "system"
    else:
        kind = "ai" if actor == "agent" else "system"
    return send_interview_link(db, interview, candidate, intro, kind,
                               subject="" if kind == "ai" else f"Interview rescheduled: {interview.job.title}")


def send_due_reminders(db: Session) -> int:
    """Remind candidates shortly before their interview. Called by the scheduler."""
    s = get_settings()
    if s.interview_reminder_minutes <= 0:
        return 0
    now = utcnow()
    window = timedelta(minutes=s.interview_reminder_minutes)
    due = db.scalars(
        select(Interview).where(
            Interview.status == InterviewStatus.scheduled.value,
            Interview.reminder_sent_at.is_(None),
            Interview.scheduled_at <= now + window,
            Interview.scheduled_at > now,
        )
    ).all()
    count = 0
    for iv in due:
        iv.reminder_sent_at = now
        # No reminder if the link itself went out within the reminder window.
        if iv.created_at > iv.scheduled_at - window:
            continue
        c = iv.candidate
        engagement = db.get(Engagement, iv.engagement_id) if iv.engagement_id else None
        minutes = max(1, int((iv.scheduled_at - now).total_seconds() // 60))
        body = f"Reminder: your interview starts in about {minutes} minutes.\n{interview_url(iv)}"
        # The candidate chose this time, so quiet hours don't apply to the reminder.
        messaging.submit_outbound(db, c, engagement, iv.channel, body, "system", skip_quiet_hours=True,
                                  subject="" if engagement else "Interview reminder")
        log_activity(db, c.id, "interview_reminder", "Interview reminder sent", detail={"interview_id": iv.id})
        count += 1
    db.commit()
    return count
