"""Speech-to-text and text-to-speech for the interview chat, via Gnani (api.vachana.ai).

All calls go through `get_speech()`, so tests can swap in a fake with
`set_speech()`. Audio is passed straight through: never stored or logged.
"""

import logging
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_STT_SECONDS = 60  # Gnani's REST STT limit
MAX_TTS_CHARS = 2000


class SpeechError(RuntimeError):
    pass


class Speech(Protocol):
    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str: ...

    async def synthesize(self, text: str) -> tuple[bytes, str]: ...


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{resp.status_code} {err.get('type', '')}: {str(err.get('message', ''))[:200]}"
    except Exception:
        return f"{resp.status_code}"


class GnaniSpeech:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        if not s.gnani_api_key:
            raise SpeechError("GNANI_API_KEY is not set.")
        self.s = s
        self._client = httpx.AsyncClient(
            base_url=s.gnani_base_url, timeout=s.gnani_timeout_seconds, transport=transport,
            headers={"X-API-Key-ID": s.gnani_api_key},
        )

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        try:
            resp = await self._client.post(
                "/stt/v3",
                files={"audio_file": (filename, audio, content_type)},
                data={"language_code": self.s.gnani_language, "format": "transcribe"},
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"Speech-to-text request failed ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise SpeechError(f"Speech-to-text failed: {_error_message(resp)}")
        data = resp.json()
        if not data.get("success", True):
            raise SpeechError("Speech-to-text failed: provider reported no success")
        return str(data.get("transcript") or "").strip()

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        body = {
            "text": text[:MAX_TTS_CHARS],
            "voice": self.s.gnani_tts_voice,
            "model": self.s.gnani_tts_model,
            "language": self.s.gnani_language,
            "speed": 1.0,
            "audio_config": {"sample_rate": 24000, "num_channels": 1, "container": "mp3", "bitrate": "64k"},
        }
        try:
            resp = await self._client.post("/api/v1/tts/inference", json=body)
        except httpx.HTTPError as exc:
            raise SpeechError(f"Text-to-speech request failed ({type(exc).__name__})") from exc
        if resp.status_code != 200 or not resp.content:
            raise SpeechError(f"Text-to-speech failed: {_error_message(resp)}")
        return resp.content, resp.headers.get("content-type", "audio/mpeg").split(";")[0]


_speech: Speech | None = None


def get_speech() -> Speech:
    global _speech
    if _speech is None:
        _speech = GnaniSpeech()
    return _speech


def set_speech(speech: Speech | None) -> None:
    """Override the speech client (tests) or reset it (None)."""
    global _speech
    _speech = speech
