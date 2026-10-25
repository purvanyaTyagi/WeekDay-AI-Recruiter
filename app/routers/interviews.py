"""Recruiter-side interview routes."""

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import csrf_protect, require_login
from app.db import get_db
from app.interview import session as interview_session
from app.interview import summary
from app.models import Candidate, Channel, Engagement, Interview, InterviewStatus, Job, utcnow
from app.services import handoff, messaging
from app.templating import flash, localdt, templates, tz_or_default

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])


def parse_local(value: str, tz_name: str) -> datetime | None:
    """Parse an <input type=datetime-local> value in the given timezone, return UTC."""
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=tz_or_default(tz_name))
    return naive.astimezone(utcnow().tzinfo)


@router.post("/candidates/{cid}/interviews")
def schedule(request: Request, cid: int, job_id: int = Form(...), start_local: str = Form(...),
             channel: str = Form(...), db: Session = Depends(get_db)):
    c = db.get(Candidate, cid)
    job = db.get(Job, job_id)
    if not c or not job:
        raise HTTPException(404)
    start = parse_local(start_local, c.timezone)
    if start is None or start < utcnow() - timedelta(minutes=1):
        flash(request, "Pick a valid start time in the future.", "error")
        return RedirectResponse(f"/candidates/{cid}", status_code=303)
    if channel not in (Channel.email.value, Channel.sms.value) or \
            (channel == "email" and not c.email) or (channel == "sms" and not c.phone):
        flash(request, "The candidate has no contact details for that channel.", "error")
        return RedirectResponse(f"/candidates/{cid}", status_code=303)
    if handoff.active_interview(db, c.id, job.id):
        flash(request, "This candidate already has a scheduled interview for this job; reschedule it instead.",
              "error")
        return RedirectResponse(f"/candidates/{cid}", status_code=303)
    handoff.schedule_manually(db, c, job, start, channel)
    db.commit()
    flash(request, "Interview scheduled and link sent (or queued). See the conversation for details.", "success")
    return RedirectResponse(f"/candidates/{cid}", status_code=303)


# ---------- Interviews channel ----------

STATUSES = [s.value for s in InterviewStatus]


def _get(db: Session, iid: int) -> Interview:
    iv = db.get(Interview, iid)
    if not iv:
        raise HTTPException(404, "Interview not found")
    return iv


def _day_bounds(value: str, end: bool) -> datetime | None:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    t = time.max if end else time.min
    return datetime.combine(d, t, tzinfo=tz_or_default(None)).astimezone(utcnow().tzinfo)


@router.get("/interviews")
def list_interviews(request: Request, status: str = "", date_from: str = "", date_to: str = "",
                    db: Session = Depends(get_db)):
    stmt = select(Interview).order_by(Interview.scheduled_at.desc())
    if status in STATUSES:
        stmt = stmt.where(Interview.status == status)
    start = _day_bounds(date_from, end=False) if date_from else None
    end = _day_bounds(date_to, end=True) if date_to else None
    if start:
        stmt = stmt.where(Interview.scheduled_at >= start)
    if end:
        stmt = stmt.where(Interview.scheduled_at <= end)
    interviews = list(db.scalars(stmt))
    counts = dict(db.execute(select(Interview.status, func.count()).group_by(Interview.status)).all())
    return templates.TemplateResponse(request, "interviews/list.html", {
        "interviews": interviews, "status": status, "date_from": date_from, "date_to": date_to,
        "statuses": STATUSES, "counts": counts, "interview_url": handoff.interview_url,
    })


@router.get("/interviews/{iid}")
def interview_detail(request: Request, iid: int, db: Session = Depends(get_db)):
    iv = _get(db, iid)
    return templates.TemplateResponse(request, "interviews/detail.html", {
        "iv": iv, "url": handoff.interview_url(iv), "busy": interview_session.is_busy(iv.id),
    })


@router.post("/interviews/{iid}/cancel")
def cancel(request: Request, iid: int, notify: str = Form(""), db: Session = Depends(get_db)):
    iv = _get(db, iid)
    if iv.status != InterviewStatus.scheduled.value:
        flash(request, "Only scheduled interviews can be cancelled. Use “End interview” for one in progress.", "error")
        return RedirectResponse(f"/interviews/{iid}", status_code=303)
    interview_session.end_interview(db, iv, InterviewStatus.cancelled.value, "cancelled by recruiter",
                                    actor="recruiter")
    if notify:
        engagement = db.get(Engagement, iv.engagement_id) if iv.engagement_id else None
        when = localdt(iv.scheduled_at, iv.candidate.timezone, "%A %d %B, %H:%M %Z")
        messaging.submit_outbound(db, iv.candidate, engagement, iv.channel,
                                  f"Your interview for {iv.job.title} on {when} has been cancelled. "
                                  f"We'll be in touch if anything changes.", "system",
                                  subject="" if engagement else "Interview cancelled")
    db.commit()
    flash(request, "Interview cancelled." + (" The candidate was notified." if notify else ""), "success")
    return RedirectResponse(f"/interviews/{iid}", status_code=303)


@router.post("/interviews/{iid}/reschedule")
def reschedule(request: Request, iid: int, start_local: str = Form(...), db: Session = Depends(get_db)):
    iv = _get(db, iid)
    start = parse_local(start_local, iv.candidate.timezone)
    if start is None or start < utcnow():
        flash(request, "Pick a valid start time in the future.", "error")
        return RedirectResponse(f"/interviews/{iid}", status_code=303)
    try:
        msg = handoff.reschedule_interview(db, iv, start, actor="recruiter")
    except ValueError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/interviews/{iid}", status_code=303)
    db.commit()
    flash(request, f"Rescheduled. New link message: {msg.status.replace('_', ' ')}.", "success")
    return RedirectResponse(f"/interviews/{iid}", status_code=303)


@router.post("/interviews/{iid}/end")
def end_early(request: Request, iid: int, db: Session = Depends(get_db)):
    iv = _get(db, iid)
    if iv.status != InterviewStatus.in_progress.value:
        flash(request, "Only an interview in progress can be ended.", "error")
        return RedirectResponse(f"/interviews/{iid}", status_code=303)
    interview_session.end_interview(db, iv, InterviewStatus.incomplete.value, "ended early by recruiter",
                                    actor="recruiter")
    flash(request, "Interview ended. A summary of what was covered is being generated.", "success")
    return RedirectResponse(f"/interviews/{iid}", status_code=303)


@router.post("/interviews/{iid}/summary")
def regenerate_summary(request: Request, iid: int, db: Session = Depends(get_db)):
    iv = _get(db, iid)
    if iv.status not in (InterviewStatus.completed.value, InterviewStatus.incomplete.value):
        flash(request, "Summaries are only available after the interview.", "error")
    else:
        summary.request_summary(iv.id)
        flash(request, "Generating the summary. Refresh in a moment.", "success")
    return RedirectResponse(f"/interviews/{iid}", status_code=303)
