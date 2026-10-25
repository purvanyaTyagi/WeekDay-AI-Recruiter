"""Outbound message pipeline. Every outbound message goes through `submit_outbound`.

Kinds:
  ai     - drafted by the Outreach Agent. Subject to pause, send mode, daily limit, quiet hours.
  human  - written or approved by the recruiter. Subject to opt-out and quiet hours.
  system - templated system notices (interview link, reminder). Subject to opt-out;
           reminders may skip quiet hours because the candidate picked that time.
"""

import logging
from email.utils import make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import get_email_adapter, get_sms_adapter
from app.adapters.email.base import OutgoingEmail
from app.config import get_settings
from app.models import (
    Approval,
    Candidate,
    Channel,
    Direction,
    Engagement,
    Message,
    MessageStatus,
    utcnow,
)
from app.services import safety
from app.services.activity import log_activity
from app.templating import localdt

log = logging.getLogger(__name__)


class SendBlocked(Exception):
    pass


def _thread_info(db: Session, msg: Message) -> tuple[str, list[str], str]:
    """(in_reply_to, references, subject) for an outbound email in an engagement thread."""
    if not msg.engagement_id:
        return "", [], msg.subject
    prior = list(
        db.scalars(
            select(Message)
            .where(
                Message.engagement_id == msg.engagement_id,
                Message.channel == Channel.email.value,
                Message.provider_message_id != "",
                Message.id != msg.id,
            )
            .order_by(Message.created_at)
        )
    )
    refs = [m.provider_message_id for m in prior]
    in_reply_to = refs[-1] if refs else ""
    subject = msg.subject
    if prior and not subject:
        first_subject = next((m.subject for m in prior if m.subject), "")
        if first_subject:
            subject = first_subject if first_subject.lower().startswith("re:") else f"Re: {first_subject}"
    return in_reply_to, refs, subject


def _send_now(db: Session, msg: Message) -> None:
    c = msg.candidate
    try:
        if msg.channel == Channel.email.value:
            if not c.email:
                raise SendBlocked("Candidate has no email address.")
            in_reply_to, refs, subject = _thread_info(db, msg)
            msg.subject = subject or f"Opportunity at {get_settings().company_name}"
            domain = (get_settings().email_from or "localhost@localhost").split("@")[-1]
            message_id = make_msgid(domain=domain)
            msg.provider_message_id = get_email_adapter().send(
                OutgoingEmail(
                    to=c.email,
                    subject=msg.subject,
                    body=msg.body,
                    message_id=message_id,
                    in_reply_to=in_reply_to,
                    references=refs,
                )
            )
            msg.in_reply_to = in_reply_to
        else:
            if not c.phone:
                raise SendBlocked("Candidate has no mobile number.")
            msg.provider_message_id = get_sms_adapter().send(c.phone, msg.body)
    except Exception as exc:
        msg.status = MessageStatus.failed.value
        msg.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        c.needs_attention = True
        c.attention_reason = f"A message failed to send ({msg.channel}): {msg.error[:200]}"
        log.error("Send failed for message %s: %s", msg.id, type(exc).__name__)
        log_activity(db, c.id, "send_failed", f"{msg.channel.upper()} failed to send: {msg.error[:200]}")
        return
    msg.status = MessageStatus.sent.value
    msg.sent_at = utcnow()
    msg.hold_reason = ""
    actor = {Approval.auto.value: "ai", Approval.none.value: "system"}.get(msg.approval, "recruiter")
    how = {
        Approval.auto.value: "auto-sent by AI",
        Approval.human_approved.value: "AI draft approved by recruiter",
        Approval.human_written.value: "written by recruiter",
    }.get(msg.approval, "system message")
    log_activity(
        db,
        c.id,
        "message_sent",
        f"{msg.channel.upper()} sent ({how})",
        actor=actor,
        detail={"message_id": msg.id},
    )


def _hold(db: Session, msg: Message, reason: str) -> None:
    msg.status = MessageStatus.pending_review.value
    msg.hold_reason = reason
    log_activity(
        db,
        msg.candidate_id,
        "message_held",
        f"Draft held for review: {reason}",
        detail={"message_id": msg.id},
    )


def _queue_for_quiet_hours(db: Session, msg: Message, until) -> None:
    msg.status = MessageStatus.queued.value
    msg.hold_reason = f"Quiet hours; will send after {localdt(until, msg.candidate.timezone)}"
    log_activity(db, msg.candidate_id, "message_queued", msg.hold_reason, detail={"message_id": msg.id})


def dispatch(db: Session, msg: Message, kind: str, skip_quiet_hours: bool = False) -> Message:
    """Decide whether a message is sent now, queued for quiet hours, or held for review."""
    c: Candidate = msg.candidate
    if c.opted_out:
        _hold(db, msg, "Candidate opted out. Clear the opt-out before sending anything.")
        return msg
    if kind == "ai":
        if c.agent_paused:
            _hold(db, msg, "Agent is paused for this candidate.")
            return msg
        if safety.effective_send_mode(db, c) == "review":
            _hold(db, msg, "Review-before-send mode is on.")
            return msg
        if safety.over_daily_limit(db, c.id):
            _hold(db, msg, "Daily message limit reached for this candidate.")
            return msg
    until = None if skip_quiet_hours else safety.quiet_hours_until(db, c)
    if until:
        _queue_for_quiet_hours(db, msg, until)
        return msg
    _send_now(db, msg)
    return msg


def submit_outbound(
    db: Session,
    candidate: Candidate,
    engagement: Engagement | None,
    channel: str,
    body: str,
    kind: str,
    subject: str = "",
    skip_quiet_hours: bool = False,
    hold_reason: str = "",
) -> Message:
    approval = {
        "ai": Approval.auto.value,  # becomes human_approved if a recruiter approves it later
        "human": Approval.human_written.value,
        "system": Approval.none.value,
    }[kind]
    msg = Message(
        candidate=candidate,
        engagement=engagement,
        direction=Direction.outbound.value,
        channel=channel,
        subject=subject,
        body=body,
        status=MessageStatus.pending_review.value,
        ai_generated=(kind == "ai"),
        approval=approval,
    )
    db.add(msg)
    db.flush()
    if hold_reason:
        _hold(db, msg, hold_reason)
        return msg
    return dispatch(db, msg, kind, skip_quiet_hours=skip_quiet_hours)


def approve_and_send(db: Session, msg: Message, body: str | None = None, subject: str | None = None) -> Message:
    """Recruiter approves an AI draft (optionally edited)."""
    if msg.status != MessageStatus.pending_review.value:
        raise SendBlocked("Only drafts waiting for review can be approved.")
    if body is not None and body.strip():
        msg.body = body.strip()
    if subject is not None and msg.channel == Channel.email.value:
        msg.subject = subject.strip()
    msg.approval = Approval.human_approved.value if msg.ai_generated else Approval.human_written.value
    log_activity(db, msg.candidate_id, "message_approved", "Recruiter approved a draft", actor="recruiter",
                 detail={"message_id": msg.id})
    return dispatch(db, msg, "human")


def discard(db: Session, msg: Message) -> None:
    if msg.status not in (MessageStatus.pending_review.value, MessageStatus.queued.value, MessageStatus.failed.value):
        raise SendBlocked("This message can no longer be discarded.")
    msg.status = MessageStatus.discarded.value
    log_activity(db, msg.candidate_id, "message_discarded", "Recruiter discarded a draft", actor="recruiter",
                 detail={"message_id": msg.id})


def flush_queued(db: Session) -> int:
    """Send queued messages whose quiet hours have ended. Called by the scheduler."""
    sent = 0
    for msg in db.scalars(select(Message).where(Message.status == MessageStatus.queued.value)):
        c = msg.candidate
        if c.opted_out:
            _hold(db, msg, "Candidate opted out while the message was queued.")
            continue
        if msg.approval == Approval.auto.value and c.agent_paused:
            _hold(db, msg, "Agent was paused while the message was queued.")
            continue
        if safety.quiet_hours_until(db, c):
            continue
        _send_now(db, msg)
        sent += msg.status == MessageStatus.sent.value
    db.commit()
    return sent
