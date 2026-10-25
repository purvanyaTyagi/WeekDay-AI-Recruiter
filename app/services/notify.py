"""Recruiter notifications: in-app flag + activity log, plus an optional email."""

import logging
from email.utils import make_msgid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Candidate
from app.services.activity import log_activity

log = logging.getLogger(__name__)


def notify_recruiter(db: Session, candidate: Candidate, kind: str, text: str) -> None:
    candidate.needs_attention = True
    candidate.attention_reason = text[:1000]
    log_activity(db, candidate.id, kind, text, actor="system")
    s = get_settings()
    if not s.recruiter_notify_email:
        return
    from app.adapters import get_email_adapter
    from app.adapters.email.base import OutgoingEmail

    try:
        get_email_adapter().send(OutgoingEmail(
            to=s.recruiter_notify_email,
            subject=f"[{s.app_name}] {candidate.full_name}: {kind.replace('_', ' ')}",
            body=f"{text}\n\n{s.base_url.rstrip('/')}/candidates/{candidate.id}",
            message_id=make_msgid(domain=(s.email_from or "localhost@localhost").split("@")[-1]),
        ))
    except Exception as exc:
        log.error("Recruiter notification email failed: %s", type(exc).__name__)
