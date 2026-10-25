"""Provider webhooks (no login; authenticated by provider signature)."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response

from app.config import get_settings
from app.db import SessionLocal
from app.models import Candidate, Channel
from app.services import outreach_agent
from app.services.inbound import find_candidate_by_phone, record_inbound

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _reply_in_background(candidate_id: int, engagement_id: int) -> None:
    db = SessionLocal()
    try:
        c = db.get(Candidate, candidate_id)
        eng = next((e for e in c.engagements if e.id == engagement_id), None) if c else None
        if c and eng:
            outreach_agent.reply_to_candidate(db, c, eng)
    except Exception:
        db.rollback()
        log.exception("Background agent reply failed")
    finally:
        db.close()


@router.post("/twilio/sms")
async def twilio_sms(request: Request, background: BackgroundTasks):
    s = get_settings()
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if s.twilio_validate_signature:
        from app.adapters.sms.twilio import validate_signature

        # Twilio signs the public URL it called, which behind a tunnel or proxy
        # differs from request.url, so rebuild it from BASE_URL.
        url = s.base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        if not s.twilio_auth_token or not validate_signature(
            s, url, params, request.headers.get("x-twilio-signature", "")
        ):
            raise HTTPException(403, "Invalid Twilio signature")

    from_number, body, sid = params.get("From", ""), params.get("Body", ""), params.get("MessageSid", "")
    db = SessionLocal()
    try:
        candidate = find_candidate_by_phone(db, from_number)
        if candidate is None:
            log.info("SMS from unknown number ignored")
            return Response(EMPTY_TWIML, media_type="application/xml")
        _, engagement, should_reply = record_inbound(
            db, candidate, Channel.sms.value, body or "(empty message)", provider_message_id=sid
        )
        db.commit()
        if should_reply and engagement is not None:
            # Answer Twilio fast; the model call happens after the response.
            background.add_task(_reply_in_background, candidate.id, engagement.id)
    finally:
        db.close()
    return Response(EMPTY_TWIML, media_type="application/xml")
