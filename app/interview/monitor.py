"""Scheduler checks for interviews: no-shows and abandoned sessions."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.interview import session as interview_session
from app.models import Interview, InterviewStatus, utcnow
from app.services.notify import notify_recruiter
from app.templating import localdt


def check_interviews(db: Session) -> dict:
    s = get_settings()
    now = utcnow()
    counts = {"no_show": 0, "incomplete": 0}

    no_shows = db.scalars(select(Interview).where(
        Interview.status == InterviewStatus.scheduled.value,
        Interview.scheduled_at < now - timedelta(minutes=s.interview_no_show_minutes),
    )).all()
    for iv in no_shows:
        interview_session.end_interview(db, iv, InterviewStatus.no_show.value,
                                        f"candidate did not join within {s.interview_no_show_minutes} minutes",
                                        actor="system")
        notify_recruiter(db, iv.candidate, "interview_no_show",
                         f"No-show: {iv.candidate.full_name} did not join the interview for {iv.job.title} "
                         f"scheduled {localdt(iv.scheduled_at)}. You can reschedule it from the Interviews page.")
        counts["no_show"] += 1

    running = db.scalars(select(Interview).where(Interview.status == InterviewStatus.in_progress.value)).all()
    for iv in running:
        if interview_session.is_busy(iv.id):
            continue
        idle = now - (iv.last_activity_at or iv.started_at or now)
        overrun = iv.started_at and now - iv.started_at > timedelta(minutes=s.interview_max_minutes * 2)
        if idle > timedelta(minutes=s.interview_abandon_minutes) or overrun:
            reason = (f"no activity for {s.interview_abandon_minutes} minutes" if not overrun
                      else "ran far past the time limit")
            interview_session.end_interview(db, iv, InterviewStatus.incomplete.value, reason, actor="system")
            notify_recruiter(db, iv.candidate, "interview_incomplete",
                             f"Incomplete interview: {iv.candidate.full_name} stopped responding during the "
                             f"{iv.job.title} interview ({reason}). A partial summary will be generated.")
            counts["incomplete"] += 1
    db.commit()
    return counts
