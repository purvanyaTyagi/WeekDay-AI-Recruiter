"""Candidate-facing interview page. No login: the unguessable token is the credential."""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.interview import session as interview_session
from app.interview.channel import broker, entry_dict
from app.interview.engine import MAX_CANDIDATE_CHARS, InterviewEngine, window_state
from app.models import Interview, InterviewStatus, TranscriptEntry
from app.services.speech import MAX_STT_SECONDS, SpeechError, get_speech
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/i")

NO_INDEX = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def _by_token(db: Session, token: str) -> Interview:
    if not token or len(token) > 100:
        raise HTTPException(404)
    iv = db.scalar(select(Interview).where(Interview.token == token))
    if iv is None:
        raise HTTPException(404, "Interview not found")
    return iv


def _snapshot(db: Session, iv: Interview, after: int = 0) -> dict:
    return {
        "status": iv.status,
        "entries": [entry_dict(e) for e in InterviewEngine().transcript(db, iv) if e.role != "system" and e.seq > after],
        "partial": broker.partial.get(iv.id, ""),
        "generating": interview_session.is_busy(iv.id),
        "needs_turn": interview_session.needs_agent_turn(db, iv) and not interview_session.is_busy(iv.id),
        "closed_message": interview_session.CLOSED_MESSAGES.get(iv.status, ""),
    }


@router.get("/{token}")
def interview_page(request: Request, token: str, db: Session = Depends(get_db)):
    iv = _by_token(db, token)
    state = window_state(iv)
    ctx = {
        "iv": iv,
        "state": state,
        "candidate_name": iv.candidate.full_name.split()[0] if iv.candidate.full_name else "",
        "max_chars": MAX_CANDIDATE_CHARS,
        "voice": get_settings().voice_configured,
        "max_record_seconds": MAX_STT_SECONDS - 5,
    }
    return templates.TemplateResponse(request, "interview/page.html", ctx, headers=NO_INDEX)


@router.post("/{token}/start")
async def start_interview(token: str, request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("consent"):
        return JSONResponse({"error": "Please confirm you understand the interview is recorded."}, status_code=400)
    db = SessionLocal()
    try:
        iv = _by_token(db, token)
        state, iv_id = window_state(iv), iv.id
    finally:
        db.close()
    if state == "in_progress":
        return {"ok": True}
    if state != "open":
        return JSONResponse({"error": "This interview can't be started right now.", "state": state}, status_code=409)
    await interview_session.start(iv_id)
    return {"ok": True}


@router.post("/{token}/message")
async def send_message(token: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str(body.get("text", ""))[: MAX_CANDIDATE_CHARS + 100]
    db = SessionLocal()
    try:
        iv_id = _by_token(db, token).id
    finally:
        db.close()
    result = await interview_session.candidate_says(iv_id, text)
    codes = {"ok": 200, "busy": 409, "closed": 410, "empty": 400}
    messages = {"busy": "Please wait for the interviewer to finish.", "closed": "This interview has ended.",
                "empty": "Type a message first."}
    return JSONResponse({"result": result, "error": messages.get(result)}, status_code=codes[result])


@router.post("/{token}/retry")
async def retry_turn(token: str):
    db = SessionLocal()
    try:
        iv_id = _by_token(db, token).id
    finally:
        db.close()
    return {"result": await interview_session.retry(iv_id)}


@router.get("/{token}/events")
async def events(token: str, request: Request):
    db = SessionLocal()
    try:
        iv = _by_token(db, token)
        iv_id = iv.id
        queue = broker.subscribe(iv_id)  # subscribe before the snapshot so no event is missed
        snapshot = _snapshot(db, iv)
    finally:
        db.close()

    async def stream():
        import json

        try:
            yield {"event": "snapshot", "data": json.dumps(snapshot)}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    continue
                yield item
        finally:
            broker.unsubscribe(iv_id, queue)

    return EventSourceResponse(stream(), ping=15, headers=NO_INDEX)


@router.get("/{token}/poll")
def poll(token: str, after: int = 0, db: Session = Depends(get_db)):
    """Fallback for networks that buffer SSE (e.g. Cloudflare quick tunnels): same data as the snapshot,
    with only the entries after seq `after`. The partial text is always the whole turn so far."""
    return JSONResponse(_snapshot(db, _by_token(db, token), after), headers=NO_INDEX)


MAX_AUDIO_BYTES = 3 * 1024 * 1024  # ~60 s of 16 kHz mono WAV, which is what the page sends


@router.post("/{token}/transcribe")
async def transcribe(token: str, audio: UploadFile = File(...)):
    """Candidate's dictated answer -> text for the input box. The candidate reviews it before sending."""
    if not get_settings().voice_configured:
        raise HTTPException(404)
    db = SessionLocal()
    try:
        status = _by_token(db, token).status
    finally:
        db.close()
    if status != InterviewStatus.in_progress.value:
        return JSONResponse({"error": "This interview has ended."}, status_code=410)
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if not data:
        return JSONResponse({"error": "No audio was recorded."}, status_code=400)
    if len(data) > MAX_AUDIO_BYTES:
        return JSONResponse({"error": f"Recordings can be up to {MAX_STT_SECONDS} seconds."}, status_code=413)
    try:
        text = await get_speech().transcribe(data, audio.filename or "answer.wav", audio.content_type or "audio/wav")
    except SpeechError as exc:
        log.warning("Interview transcription failed: %s", str(exc)[:200])
        return JSONResponse({"error": "Couldn't transcribe that. Please try again or type your answer."},
                            status_code=502)
    if not text:
        return JSONResponse({"error": "No speech was recognised. Please try again."}, status_code=422)
    return {"text": text}


@router.post("/{token}/speak/{seq}")
async def speak(token: str, seq: int):
    """Read one of the interviewer's messages aloud. Only saved agent messages can be spoken."""
    if not get_settings().voice_configured:
        raise HTTPException(404)
    db = SessionLocal()
    try:
        iv = _by_token(db, token)
        if iv.status in (InterviewStatus.scheduled.value, InterviewStatus.cancelled.value,
                         InterviewStatus.no_show.value):
            raise HTTPException(410)
        entry = db.scalar(select(TranscriptEntry).where(TranscriptEntry.interview_id == iv.id,
                                                        TranscriptEntry.seq == seq, TranscriptEntry.role == "agent"))
        if entry is None:
            raise HTTPException(404)
        text = entry.content
    finally:
        db.close()
    try:
        audio, content_type = await get_speech().synthesize(text)
    except SpeechError as exc:
        log.warning("Interview speech synthesis failed: %s", str(exc)[:200])
        return JSONResponse({"error": "Couldn't play this message aloud."}, status_code=502)
    return Response(audio, media_type=content_type, headers=NO_INDEX)


@router.get("/{token}/state")
def state(token: str, db: Session = Depends(get_db)):
    iv = _by_token(db, token)
    return {"state": window_state(iv), "status": iv.status,
            "closed": iv.status not in (InterviewStatus.scheduled.value, InterviewStatus.in_progress.value)}
