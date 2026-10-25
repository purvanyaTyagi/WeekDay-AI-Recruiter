"""Voice in the interview chat: Gnani client request shape and the /transcribe and /speak routes."""

import json

import httpx
import pytest

from app.config import get_settings
from app.models import Engagement, Interview, TranscriptEntry, utcnow
from app.services.speech import GnaniSpeech, SpeechError


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "gnani_api_key", "gnani-test-key")


@pytest.fixture
def live_interview(db, candidate_job):
    c, job = candidate_job
    eng = Engagement(candidate_id=c.id, job_id=job.id, channel="email")
    db.add(eng)
    db.flush()
    iv = Interview(candidate_id=c.id, job_id=job.id, engagement_id=eng.id, token="tok-" + "v" * 40,
                   scheduled_at=utcnow(), status="in_progress", started_at=utcnow(), channel="email")
    db.add(iv)
    db.flush()
    db.add_all([TranscriptEntry(interview_id=iv.id, seq=1, role="agent", content="Tell me about FastAPI?"),
                TranscriptEntry(interview_id=iv.id, seq=2, role="candidate", content="I used it a lot.")])
    db.commit()
    return iv


# ---------- Gnani client ----------


async def test_gnani_request_shapes(voice_on):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/stt/v3":
            return httpx.Response(200, json={"success": True, "transcript": " Hello there "})
        return httpx.Response(200, content=b"mp3bytes", headers={"content-type": "audio/mpeg"})

    sp = GnaniSpeech(transport=httpx.MockTransport(handler))
    assert await sp.transcribe(b"RIFF....", "answer.wav", "audio/wav") == "Hello there"
    assert await sp.synthesize("Hi Jane.") == (b"mp3bytes", "audio/mpeg")

    stt, tts = seen
    assert str(stt.url) == "https://api.vachana.ai/stt/v3"
    assert stt.headers["x-api-key-id"] == "gnani-test-key"
    body = stt.content
    assert b'name="audio_file"; filename="answer.wav"' in body and b'name="language_code"' in body
    assert b"en-IN" in body
    assert str(tts.url) == "https://api.vachana.ai/api/v1/tts/inference"
    payload = json.loads(tts.content)
    assert payload["text"] == "Hi Jane." and payload["voice"] == "Kaveri" and payload["model"] == "timbre-v2.5"
    assert payload["language"] == "en-IN" and payload["audio_config"]["container"] == "mp3"


async def test_gnani_errors_raise_speech_error(voice_on):
    sp = GnaniSpeech(transport=httpx.MockTransport(lambda r: httpx.Response(
        403, json={"success": False, "error": {"type": "FORBIDDEN", "message": "no credits"}})))
    with pytest.raises(SpeechError, match="FORBIDDEN"):
        await sp.transcribe(b"x", "a.wav", "audio/wav")
    with pytest.raises(SpeechError):
        await sp.synthesize("hi")


def test_gnani_requires_key():
    with pytest.raises(SpeechError):
        GnaniSpeech()


# ---------- routes ----------


def test_voice_hidden_without_key(client, live_interview):
    t = live_interview.token
    assert "mic-btn" not in client.get(f"/i/{t}").text
    assert client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")}).status_code == 404
    assert client.post(f"/i/{t}/speak/1").status_code == 404


def test_page_shows_voice_and_disclosure(client, db, live_interview, voice_on):
    assert "mic-btn" in client.get(f"/i/{live_interview.token}").text
    live_interview.status = "scheduled"
    db.commit()
    page = client.get(f"/i/{live_interview.token}").text
    assert "Gnani.ai" in page and "not stored" in page


def test_transcribe(client, db, live_interview, voice_on, fakes):
    t = live_interview.token
    r = client.post(f"/i/{t}/transcribe", files={"audio": ("answer.wav", b"RIFF" + b"\0" * 100, "audio/wav")})
    assert r.status_code == 200 and r.json() == {"text": "I built the billing service in FastAPI."}
    assert fakes.speech.calls == [("stt", 104, "audio/wav")]
    # Dictation never enters the transcript by itself: the candidate still has to send it.
    assert [e.role for e in db.get(Interview, live_interview.id).transcript] == ["agent", "candidate"]

    assert client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", b"", "audio/wav")}).status_code == 400
    big = b"\0" * (3 * 1024 * 1024 + 1)
    assert client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", big, "audio/wav")}).status_code == 413
    fakes.speech.transcript = ""
    assert client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")}).status_code == 422
    fakes.speech.error = SpeechError("down")
    r = client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")})
    assert r.status_code == 502 and "type your answer" in r.json()["error"]

    live_interview.status = "completed"
    db.commit()
    assert client.post(f"/i/{t}/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")}).status_code == 410


def test_speak_only_agent_messages(client, db, live_interview, voice_on, fakes):
    t = live_interview.token
    r = client.post(f"/i/{t}/speak/1")
    assert r.status_code == 200 and r.content == b"ID3fake-mp3" and r.headers["content-type"] == "audio/mpeg"
    assert fakes.speech.calls == [("tts", "Tell me about FastAPI?")]
    assert client.post(f"/i/{t}/speak/2").status_code == 404  # candidate's message
    assert client.post(f"/i/{t}/speak/99").status_code == 404
    assert client.post("/i/not-a-real-token/speak/1").status_code == 404

    live_interview.status = "completed"  # the closing message can still be read aloud
    db.commit()
    assert client.post(f"/i/{t}/speak/1").status_code == 200
    fakes.speech.error = SpeechError("down")
    assert client.post(f"/i/{t}/speak/1").status_code == 502
    live_interview.status = "cancelled"
    db.commit()
    assert client.post(f"/i/{t}/speak/1").status_code == 410
