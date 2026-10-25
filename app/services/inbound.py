"""Inbound messages (email or SMS) from candidates."""

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Candidate,
    Channel,
    Direction,
    Engagement,
    EngagementStatus,
    Message,
    MessageStatus,
)
from app.services import outreach_agent, safety
from app.services.activity import log_activity

log = logging.getLogger(__name__)


def find_candidate_by_email(db: Session, address: str) -> Candidate | None:
    address = address.strip().lower()
    if not address:
        return None
    return db.scalar(select(Candidate).where(Candidate.email.ilike(address)).order_by(Candidate.id.desc()))


def find_candidate_by_phone(db: Session, phone: str) -> Candidate | None:
    phone = phone.strip()
    if not phone:
        return None
    return db.scalar(select(Candidate).where(Candidate.phone == phone).order_by(Candidate.id.desc()))


def _match_engagement(db: Session, candidate: Candidate, channel: str, thread_ids: list[str]) -> Engagement | None:
    ids = [t for t in thread_ids if t]
    if ids:
        m = db.scalar(
            select(Message).where(
                Message.candidate_id == candidate.id,
                Message.engagement_id.is_not(None),
                or_(*[Message.provider_message_id == t for t in ids]),
            )
        )
        if m:
            return m.engagement
    # Fall back to the most recent engagement, preferring the same channel.
    engagements = sorted(candidate.engagements, key=lambda e: e.created_at, reverse=True)
    same = [e for e in engagements if e.channel == channel]
    return (same or engagements or [None])[0]


def record_inbound(
    db: Session,
    candidate: Candidate,
    channel: str,
    body: str,
    subject: str = "",
    provider_message_id: str = "",
    thread_ids: list[str] | None = None,
) -> tuple[Message, Engagement | None, bool]:
    """Store an inbound message and apply opt-out rules.

    Returns (message, engagement, should_reply).
    """
    if provider_message_id:
        dup = db.scalar(select(Message).where(Message.provider_message_id == provider_message_id,
                                              Message.direction == Direction.inbound.value))
        if dup:
            return dup, dup.engagement, False

    engagement = _match_engagement(db, candidate, channel, thread_ids or [])
    msg = Message(
        candidate=candidate,
        engagement=engagement,
        direction=Direction.inbound.value,
        channel=channel,
        subject=subject[:500],
        body=body,
        status=MessageStatus.received.value,
        provider_message_id=provider_message_id[:500],
        in_reply_to=(thread_ids or [""])[0][:500] if thread_ids else "",
    )
    db.add(msg)
    db.flush()
    log_activity(db, candidate.id, "message_received", f"{channel.upper()} reply received", actor="candidate",
                 detail={"message_id": msg.id})

    if safety.is_opt_out(body):
        outreach_agent.mark_opted_out(db, candidate, engagement, "keyword", "stop/unsubscribe language in reply")
        return msg, engagement, False
    if candidate.opted_out:
        candidate.needs_attention = True
        candidate.attention_reason = "An opted-out candidate sent a new message. Read it and decide what to do."
        return msg, engagement, False
    if engagement is None:
        candidate.needs_attention = True
        candidate.attention_reason = "Message received from a candidate with no active outreach."
        return msg, engagement, False
    if candidate.agent_paused:
        candidate.needs_attention = True
        candidate.attention_reason = "New reply while the agent is paused."
        return msg, engagement, False
    if engagement.status in (EngagementStatus.closed.value, EngagementStatus.opted_out.value):
        candidate.needs_attention = True
        candidate.attention_reason = "New reply on a closed conversation."
        return msg, engagement, False
    return msg, engagement, True


def process_inbound(db: Session, candidate: Candidate, channel: str, body: str, allow_reply: bool = True,
                    **kwargs) -> Message:
    """Record the message, then let the Outreach Agent respond if allowed."""
    msg, engagement, should_reply = record_inbound(db, candidate, channel, body, **kwargs)
    if should_reply and not allow_reply:
        should_reply = False
        log_activity(db, candidate.id, "auto_reply_ignored",
                     "Automatic reply (out-of-office/bounce) received; the agent did not answer it")
    db.commit()
    if should_reply and engagement is not None:
        outreach_agent.reply_to_candidate(db, candidate, engagement)
    return msg


_unknown_uids: set[str] = set()  # unread mail from non-candidates; left unread, skipped this process


def poll_inbox(db: Session) -> int:
    """Fetch unread emails and hand candidate replies to the Outreach Agent."""
    from app.adapters import get_inbox_adapter

    adapter = get_inbox_adapter()
    handled = 0
    for em in adapter.fetch_new():
        if em.uid and em.uid in _unknown_uids:
            continue
        candidate = find_candidate_by_email(db, em.from_address)
        if candidate is None:
            if em.uid:
                _unknown_uids.add(em.uid)
            continue
        try:
            process_inbound(
                db,
                candidate,
                Channel.email.value,
                em.body or "(empty message)",
                allow_reply=not em.auto_reply,
                subject=em.subject,
                provider_message_id=em.message_id,
                thread_ids=[em.in_reply_to, *reversed(em.references)],
            )
        except Exception:
            db.rollback()
            log.exception("Failed to process inbound email (will retry next poll)")
            continue
        if em.uid:
            adapter.mark_seen([em.uid])
        handled += 1
    return handled


def channel_label(channel: str) -> str:
    return "SMS" if channel == Channel.sms.value else "Email"
