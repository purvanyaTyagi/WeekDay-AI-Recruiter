"""Recruiter actions on outreach: start, agent controls, review queue, manual messages."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import get_email_adapter, get_sms_adapter
from app.auth import csrf_protect, require_login
from app.db import get_db
from app.models import Candidate, Channel, Engagement, Job, Message, MessageStatus
from app.services import app_settings, messaging, outreach_agent
from app.services.activity import log_activity
from app.services.inbound import process_inbound
from app.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])


def _candidate(db: Session, cid: int) -> Candidate:
    c = db.get(Candidate, cid)
    if not c:
        raise HTTPException(404, "Candidate not found")
    return c


def _message(db: Session, mid: int) -> Message:
    m = db.get(Message, mid)
    if not m:
        raise HTTPException(404, "Message not found")
    return m


def _back(request: Request, default: str) -> RedirectResponse:
    target = request.headers.get("referer") or default
    # Only redirect within this app.
    if not target.startswith(str(request.base_url)) and not target.startswith("/"):
        target = default
    return RedirectResponse(target, status_code=303)


def _describe(msg: Message | None) -> tuple[str, str]:
    if msg is None:
        return "The agent did not send a message. Check the activity log.", "info"
    return {
        MessageStatus.sent.value: ("Message sent.", "success"),
        MessageStatus.queued.value: (f"Message queued. {msg.hold_reason}", "info"),
        MessageStatus.pending_review.value: (f"Draft waiting for your review. {msg.hold_reason}", "info"),
        MessageStatus.failed.value: (f"Sending failed: {msg.error}", "error"),
    }.get(msg.status, (f"Message status: {msg.status}", "info"))


@router.post("/candidates/{cid}/outreach")
def start_outreach(request: Request, cid: int, job_id: int = Form(...), channel: str = Form(...),
                   db: Session = Depends(get_db)):
    c = _candidate(db, cid)
    job = db.get(Job, job_id)
    if not job or channel not in (Channel.email.value, Channel.sms.value):
        flash(request, "Pick a job and a channel.", "error")
        return RedirectResponse(f"/candidates/{cid}", status_code=303)
    try:
        msg = outreach_agent.start_outreach(db, c, job, channel)
    except outreach_agent.AgentUnavailable as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/candidates/{cid}", status_code=303)
    text, kind = _describe(msg)
    if msg is None and c.needs_attention:
        text, kind = c.attention_reason, "error"
    flash(request, text, kind)
    return RedirectResponse(f"/candidates/{cid}#conversation", status_code=303)


@router.post("/candidates/{cid}/agent")
def agent_control(request: Request, cid: int, op: str = Form(...), db: Session = Depends(get_db)):
    c = _candidate(db, cid)
    if op == "pause":
        c.agent_paused = True
        log_activity(db, c.id, "agent_paused", "Agent paused", actor="recruiter")
        flash(request, "Agent paused. It will not send or draft anything for this candidate.", "success")
    elif op == "resume":
        if c.opted_out:
            flash(request, "Candidate opted out. Clear the opt-out first.", "error")
        else:
            c.agent_paused = False
            log_activity(db, c.id, "agent_resumed", "Agent resumed", actor="recruiter")
            flash(request, "Agent resumed.", "success")
    elif op == "clear_opt_out":
        c.opted_out = False
        log_activity(db, c.id, "opt_out_cleared", "Recruiter cleared the opt-out (agent stays paused)",
                     actor="recruiter")
        flash(request, "Opt-out cleared. The agent is still paused; resume it when ready.", "success")
    elif op == "resolve":
        c.needs_attention = False
        c.attention_reason = ""
        log_activity(db, c.id, "attention_resolved", "Recruiter marked the flag as resolved", actor="recruiter")
    elif op in ("mode_default", "mode_auto", "mode_review"):
        c.send_mode_override = None if op == "mode_default" else op.removeprefix("mode_")
        log_activity(db, c.id, "send_mode_changed",
                     f"Send mode set to {c.send_mode_override or 'global default'}", actor="recruiter")
        flash(request, "Send mode updated.", "success")
    else:
        raise HTTPException(400, "Unknown action")
    db.commit()
    return RedirectResponse(f"/candidates/{cid}", status_code=303)


@router.post("/candidates/{cid}/messages")
def send_manual(request: Request, cid: int, engagement_id: int = Form(...), body: str = Form(""),
                subject: str = Form(""), db: Session = Depends(get_db)):
    c = _candidate(db, cid)
    eng = db.get(Engagement, engagement_id)
    if not eng or eng.candidate_id != c.id or not body.strip():
        flash(request, "Write a message first.", "error")
        return RedirectResponse(f"/candidates/{cid}#conversation", status_code=303)
    msg = messaging.submit_outbound(db, c, eng, eng.channel, body.strip(), "human", subject=subject.strip())
    db.commit()
    flash(request, *_describe(msg))
    return RedirectResponse(f"/candidates/{cid}#conversation", status_code=303)


@router.post("/candidates/{cid}/simulate-inbound")
def simulate_inbound(request: Request, cid: int, body: str = Form(""), channel: str = Form("email"),
                     db: Session = Depends(get_db)):
    """Dev tool: pretend the candidate replied. Only allowed while console adapters are in use."""
    adapter = get_email_adapter() if channel == Channel.email.value else get_sms_adapter()
    if adapter.name != "console":
        raise HTTPException(403, "Simulation is only available with console adapters.")
    c = _candidate(db, cid)
    if not body.strip():
        flash(request, "Type a reply to simulate.", "error")
        return RedirectResponse(f"/candidates/{cid}#conversation", status_code=303)
    process_inbound(db, c, channel, body.strip())
    flash(request, "Simulated reply processed.", "success")
    return RedirectResponse(f"/candidates/{cid}#conversation", status_code=303)


@router.post("/messages/{mid}/approve")
def approve(request: Request, mid: int, body: str = Form(""), subject: str | None = Form(None),
            db: Session = Depends(get_db)):
    msg = _message(db, mid)
    try:
        messaging.approve_and_send(db, msg, body=body, subject=subject)
    except messaging.SendBlocked as e:
        flash(request, str(e), "error")
        return _back(request, f"/candidates/{msg.candidate_id}")
    db.commit()
    flash(request, *_describe(msg))
    return _back(request, f"/candidates/{msg.candidate_id}")


@router.post("/messages/{mid}/discard")
def discard(request: Request, mid: int, db: Session = Depends(get_db)):
    msg = _message(db, mid)
    try:
        messaging.discard(db, msg)
        db.commit()
        flash(request, "Draft discarded.", "success")
    except messaging.SendBlocked as e:
        flash(request, str(e), "error")
    return _back(request, f"/candidates/{msg.candidate_id}")


@router.post("/messages/{mid}/retry")
def retry(request: Request, mid: int, db: Session = Depends(get_db)):
    msg = _message(db, mid)
    if msg.status != MessageStatus.failed.value:
        flash(request, "Only failed messages can be retried.", "error")
    else:
        msg.status = MessageStatus.pending_review.value
        msg.error = ""
        messaging.approve_and_send(db, msg)
        db.commit()
        flash(request, *_describe(msg))
    return _back(request, f"/candidates/{msg.candidate_id}")


@router.get("/review")
def review_queue(request: Request, db: Session = Depends(get_db)):
    pending = list(db.scalars(select(Message).where(Message.status == MessageStatus.pending_review.value)
                              .order_by(Message.created_at)))
    queued = list(db.scalars(select(Message).where(Message.status == MessageStatus.queued.value)
                             .order_by(Message.created_at)))
    failed = list(db.scalars(select(Message).where(Message.status == MessageStatus.failed.value)
                             .order_by(Message.created_at.desc()).limit(50)))
    flagged = list(db.scalars(select(Candidate).where(Candidate.needs_attention.is_(True))))
    return templates.TemplateResponse(request, "review.html",
                                      {"pending": pending, "queued": queued, "failed": failed, "flagged": flagged})


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "settings.html", {"rs": app_settings.load(db), "errors": {},
                                                                 "email": get_email_adapter().name,
                                                                 "sms": get_sms_adapter().name})


@router.post("/settings")
def save_settings(request: Request, send_mode: str = Form(...), quiet_hours_start: str = Form(...),
                  quiet_hours_end: str = Form(...), max_per_day: int = Form(...), db: Session = Depends(get_db)):
    errors = app_settings.save(db, send_mode, quiet_hours_start, quiet_hours_end, max_per_day)
    if errors:
        db.rollback()
        rs = app_settings.RuntimeSettings(send_mode, quiet_hours_start, quiet_hours_end, max_per_day)
        return templates.TemplateResponse(request, "settings.html",
                                          {"rs": rs, "errors": errors, "email": get_email_adapter().name,
                                           "sms": get_sms_adapter().name}, status_code=422)
    db.commit()
    flash(request, "Settings saved.", "success")
    return RedirectResponse("/settings", status_code=303)
