"""Post-interview evaluation: summary, strengths, concerns, suggested score (recommendation only)."""

import logging
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.interview.engine import TAG_CLOSE, TAG_OPEN, InterviewEngine
from app.models import Interview, InterviewEvaluation
from app.services.activity import log_activity
from app.services.llm import LLMError, complete_structured, load_prompt

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="summary")
RUN_INLINE = False  # tests set this to run synchronously


class Point(BaseModel):
    point: str
    evidence: str = ""


class Evaluation(BaseModel):
    summary: str
    strengths: list[Point] = Field(default_factory=list)
    concerns: list[Point] = Field(default_factory=list)
    suggested_score: int = Field(ge=1, le=5)
    reasoning: str

    @field_validator("strengths", "concerns", mode="before")
    @classmethod
    def _coerce(cls, v):
        # Accept plain strings from models that ignore the object format.
        return [{"point": x} if isinstance(x, str) else x for x in (v or [])]


def _transcript_text(db: Session, iv: Interview) -> tuple[str, int]:
    lines, answers = [], 0
    for e in InterviewEngine().transcript(db, iv):
        t = e.created_at.strftime("%H:%M")
        if e.role == "agent":
            lines.append(f"[{t}] INTERVIEWER: {e.content}")
        elif e.role == "candidate":
            answers += 1
            lines.append(f"[{t}] CANDIDATE: {TAG_OPEN}{e.content}{TAG_CLOSE}")
        else:
            lines.append(f"[{t}] (platform) {e.content}")
    return "\n".join(lines), answers


def generate_summary(db: Session, iv: Interview) -> InterviewEvaluation:
    s = get_settings()
    ev = iv.evaluation or InterviewEvaluation(interview_id=iv.id)
    if iv.evaluation is None:
        db.add(ev)
    transcript, answers = _transcript_text(db, iv)
    ev.model = s.summary_model
    if answers == 0:
        ev.summary = "The candidate gave no answers, so there is nothing to evaluate."
        ev.strengths, ev.concerns, ev.suggested_score, ev.reasoning, ev.error = [], [], None, "", ""
        db.commit()
        return ev
    job = iv.job
    user = f"""<job>
Title: {job.title}
Required skills: {job.required_skills or "(not specified)"}
{job.description}
</job>

<briefing>
{iv.briefing or "(none)"}
</briefing>

<interview status="{iv.status}" end_reason="{iv.end_reason}">
{transcript}
</interview>

Write the evaluation JSON now."""
    messages = [{"role": "system", "content": load_prompt("summary")}, {"role": "user", "content": user}]
    try:
        result = complete_structured(s.summary_model, messages, Evaluation, temperature=0.2)
    except LLMError as exc:
        ev.error = str(exc)[:500]
        log_activity(db, iv.candidate_id, "ai_error", f"Interview summary failed: {str(exc)[:300]}", actor="ai",
                     detail={"interview_id": iv.id})
        db.commit()
        return ev
    ev.summary = result.summary
    ev.strengths = [p.model_dump() for p in result.strengths]
    ev.concerns = [p.model_dump() for p in result.concerns]
    ev.suggested_score = result.suggested_score
    ev.reasoning = result.reasoning
    ev.error = ""
    log_activity(db, iv.candidate_id, "summary",
                 f"Interview summary generated (suggested score {result.suggested_score}/5, recommendation only)",
                 actor="ai", detail={"interview_id": iv.id, "model": s.summary_model})
    db.commit()
    return ev


def _run(interview_id: int) -> None:
    db = SessionLocal()
    try:
        iv = db.get(Interview, interview_id)
        if iv is not None:
            generate_summary(db, iv)
    except Exception:
        db.rollback()
        log.exception("Summary generation failed for interview %s", interview_id)
    finally:
        db.close()


def request_summary(interview_id: int) -> None:
    """Generate the evaluation in the background (model calls are slow)."""
    if RUN_INLINE:
        _run(interview_id)
    else:
        _executor.submit(_run, interview_id)
