"""How interview messages reach the candidate.

InterviewChannel is the delivery boundary. TextChannel publishes events to
browser tabs over SSE. A voice channel would implement the same methods,
for example by sending agent text to TTS and feeding STT transcripts into
session.candidate_says(), without changing the engine.

The broker is in-memory, so live streaming needs a single app process
(see README). Transcripts are always saved to the database, so a restart
loses at most the partly streamed message.
"""

import asyncio
import json
import logging
from typing import Protocol

from app.models import TranscriptEntry

log = logging.getLogger(__name__)


class InterviewChannel(Protocol):
    async def agent_started(self) -> None: ...

    async def agent_chunk(self, text: str) -> None: ...

    async def agent_done(self, entry: TranscriptEntry) -> None: ...

    async def agent_failed(self, message: str) -> None: ...

    async def candidate_message(self, entry: TranscriptEntry) -> None: ...

    async def status(self, status: str, message: str) -> None: ...


def entry_dict(e: TranscriptEntry) -> dict:
    return {"seq": e.seq, "role": e.role, "content": e.content, "at": e.created_at.isoformat()}


class Broker:
    """Fan-out of events to the SSE connections of each interview."""

    def __init__(self):
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self.partial: dict[int, str] = {}  # text streamed so far in the current agent turn
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, interview_id: int) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(interview_id, set()).add(q)
        return q

    def unsubscribe(self, interview_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(interview_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(interview_id, None)

    def subscriber_count(self, interview_id: int) -> int:
        return len(self._subs.get(interview_id, ()))

    def publish(self, interview_id: int, event: str, data: dict) -> None:
        payload = {"event": event, "data": json.dumps(data)}
        for q in list(self._subs.get(interview_id, ())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("Dropping event for slow interview subscriber")

    def publish_threadsafe(self, interview_id: int, event: str, data: dict) -> None:
        """For callers outside the event loop (scheduler threads, sync routes)."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.publish, interview_id, event, data)


broker = Broker()


class TextChannel:
    def __init__(self, interview_id: int, b: Broker = broker):
        self.id = interview_id
        self.b = b

    async def agent_started(self) -> None:
        self.b.partial[self.id] = ""
        self.b.publish(self.id, "agent_start", {})

    async def agent_chunk(self, text: str) -> None:
        offset = len(self.b.partial.get(self.id, ""))
        self.b.partial[self.id] = self.b.partial.get(self.id, "") + text
        self.b.publish(self.id, "chunk", {"offset": offset, "text": text})

    async def agent_done(self, entry: TranscriptEntry) -> None:
        self.b.partial.pop(self.id, None)
        self.b.publish(self.id, "agent_done", {"entry": entry_dict(entry)})

    async def agent_failed(self, message: str) -> None:
        self.b.partial.pop(self.id, None)
        self.b.publish(self.id, "agent_error", {"message": message})

    async def candidate_message(self, entry: TranscriptEntry) -> None:
        self.b.publish(self.id, "candidate", {"entry": entry_dict(entry)})

    async def status(self, status: str, message: str) -> None:
        self.b.publish(self.id, "status", {"status": status, "message": message})
