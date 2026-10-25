"""Fake model client for tests. Queue responses (strings or callables) in order."""

import json
from collections.abc import Callable


class FakeLLM:
    def __init__(self):
        self.responses: list[str | Callable[[list[dict]], str]] = []
        self.calls: list[dict] = []
        self.stream_responses: list[str | Callable[[list[dict]], str]] = []

    def queue(self, *responses):
        for r in responses:
            self.responses.append(json.dumps(r) if isinstance(r, dict) else r)
        return self

    def queue_stream(self, *responses):
        self.stream_responses.extend(responses)
        return self

    def complete(self, model, messages, json_mode=False, temperature=0.4):
        self.calls.append({"model": model, "messages": messages, "json_mode": json_mode})
        if not self.responses:
            raise AssertionError("FakeLLM: no response queued")
        r = self.responses.pop(0)
        return r(messages) if callable(r) else r

    async def stream(self, model, messages, temperature=0.5):
        self.calls.append({"model": model, "messages": messages, "stream": True})
        if not self.stream_responses:
            raise AssertionError("FakeLLM: no stream response queued")
        import asyncio

        r = self.stream_responses.pop(0)
        text = r(messages) if callable(r) else r
        for i in range(0, len(text), 7):
            await asyncio.sleep(0)  # let other tasks run, like a real network stream
            yield text[i : i + 7]

    def last_user_prompt(self) -> str:
        return self.calls[-1]["messages"][-1]["content"]

    def last_system_prompt(self) -> str:
        return self.calls[-1]["messages"][0]["content"]


class FakeSpeech:
    """Fake speech provider. Set `transcript`, or `error` to an exception to raise."""

    def __init__(self):
        self.transcript = "I built the billing service in FastAPI."
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    async def transcribe(self, audio, filename, content_type):
        self.calls.append(("stt", len(audio), content_type))
        if self.error:
            raise self.error
        return self.transcript

    async def synthesize(self, text):
        self.calls.append(("tts", text))
        if self.error:
            raise self.error
        return b"ID3fake-mp3", "audio/mpeg"
