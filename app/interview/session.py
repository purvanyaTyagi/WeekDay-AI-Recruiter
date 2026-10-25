"""Runs interviews: connects the engine (what to say) to a channel (how it's delivered)."""

import asyncio
import logging

from app.db import SessionLocal
from app.interview.channel import InterviewChannel, TextChannel, broker
from app.interview.engine import InterviewEngine, MarkerFilter
from app.models import Interview, InterviewStatus
from app.services.activity import log_activity
from app.services.llm import LLMError

log = logging.getLogger(__name__)

_locks: dict[int, asyncio.Lock] = {}
_pending: set[int] = set()  # interviews with an agent turn queued or running
_tasks: set[asyncio.Task] = set()

CLOSED_MESSAGES = {
    InterviewStatus.completed.value: "The interview is complete. Thank you! The hiring team will review it and get in touch.",
    InterviewStatus.incomplete.value: "This interview has ended.",
    InterviewStatus.cancelled.value: "This interview has been cancelled.",
    InterviewStatus.no_show.value: "This interview link has expired.",
}


def _lock(interview_id: int) -> asyncio.Lock:
    return _locks.setdefault(interview_id, asyncio.Lock())


def is_busy(interview_id: int) -> bool:
    return interview_id in _pending or _lock(interview_id).locked()


def _launch_turn(interview_id: int, channel: InterviewChannel | None) -> None:
    _pending.add(interview_id)  # set synchronously so a second message is refused at once
    launch(run_agent_turn(interview_id, channel))


def launch(coro) -> asyncio.Task:
    """Run in the background, independent of the HTTP/SSE connection."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def default_channel(interview_id: int) -> InterviewChannel:
    return TextChannel(interview_id)


async def run_agent_turn(interview_id: int, channel: InterviewChannel | None = None) -> None:
    try:
        await _run_agent_turn(interview_id, channel or default_channel(interview_id))
    finally:
        _pending.discard(interview_id)


async def _run_agent_turn(interview_id: int, channel: InterviewChannel) -> None:
    engine = InterviewEngine()
    async with _lock(interview_id):
        db = SessionLocal()
        try:
            iv = db.get(Interview, interview_id)
            if iv is None or iv.status != InterviewStatus.in_progress.value:
                return
            plan = engine.plan_turn(iv)
            messages = engine.build_messages(db, iv, plan)
            await channel.agent_started()
            marker = MarkerFilter()
            parts: list[str] = []
            try:
                async for piece in engine.stream_reply(messages, marker):
                    parts.append(piece)
                    await channel.agent_chunk(piece)
            except LLMError as exc:
                log.warning("Interview %s: model error: %s", interview_id, str(exc)[:200])
                log_activity(db, iv.candidate_id, "ai_error", f"Interview Agent error: {str(exc)[:300]}",
                             actor="ai", detail={"interview_id": iv.id})
                db.commit()
                await channel.agent_failed("The interviewer had a technical problem. Click Retry to continue.")
                return
            entry, ended = engine.finish_turn(db, iv, "".join(parts), plan, marker.found)
            db.commit()
            await channel.agent_done(entry)
            if ended:
                await channel.status(iv.status, CLOSED_MESSAGES[iv.status])
                on_interview_closed(iv.id)
        finally:
            db.close()


async def start(interview_id: int, channel: InterviewChannel | None = None) -> bool:
    """Candidate pressed Start: mark in progress and let the agent open."""
    db = SessionLocal()
    try:
        iv = db.get(Interview, interview_id)
        if iv is None or iv.status != InterviewStatus.scheduled.value:
            return False
        InterviewEngine().start(db, iv)
        db.commit()
    finally:
        db.close()
    _launch_turn(interview_id, channel)
    return True


async def candidate_says(interview_id: int, text: str, channel: InterviewChannel | None = None) -> str:
    """Returns 'ok', 'busy', 'closed' or 'empty'."""
    channel = channel or default_channel(interview_id)
    if not text.strip():
        return "empty"
    if is_busy(interview_id):
        return "busy"
    db = SessionLocal()
    try:
        iv = db.get(Interview, interview_id)
        if iv is None or iv.status != InterviewStatus.in_progress.value:
            return "closed"
        entry = InterviewEngine().record_candidate(db, iv, text)
        db.commit()
        await channel.candidate_message(entry)
    finally:
        db.close()
    _launch_turn(interview_id, channel)
    return "ok"


async def retry(interview_id: int, channel: InterviewChannel | None = None) -> str:
    """Re-run the agent when the last message is the candidate's (after an error or restart)."""
    if is_busy(interview_id):
        return "busy"
    db = SessionLocal()
    try:
        iv = db.get(Interview, interview_id)
        if iv is None or not needs_agent_turn(db, iv):
            return "noop"
    finally:
        db.close()
    _launch_turn(interview_id, channel)
    return "ok"


def needs_agent_turn(db, iv: Interview) -> bool:
    """True if the last word belongs to the candidate (e.g. after a failure or restart)."""
    if iv.status != InterviewStatus.in_progress.value:
        return False
    entries = [e for e in InterviewEngine().transcript(db, iv) if e.role != "system"]
    return not entries or entries[-1].role == "candidate"


def end_interview(db, iv: Interview, status: str, reason: str, actor: str) -> None:
    """End from outside the chat (recruiter, scheduler). Safe to call from any thread."""
    InterviewEngine().end(db, iv, status, reason, actor=actor)
    db.commit()
    message = CLOSED_MESSAGES.get(status, "This interview has ended.")
    broker.publish_threadsafe(iv.id, "status", {"status": status, "message": message})
    if status in (InterviewStatus.completed.value, InterviewStatus.incomplete.value):
        on_interview_closed(iv.id)


def on_interview_closed(interview_id: int) -> None:
    """Hook for post-interview work (summary). Filled in by summary module."""
    from app.interview import summary

    summary.request_summary(interview_id)
