"""Candidate persistence: resume storage and complete deletion."""

import logging
import uuid
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Candidate, ParseStatus
from app.services.activity import log_activity
from app.services.resume_parser import parse_resume, sniff_type

log = logging.getLogger(__name__)


class UploadError(ValueError):
    pass


def _resume_abspath(rel: str) -> Path:
    base = get_settings().upload_dir.resolve()
    path = (base / rel).resolve()
    if base not in path.parents:  # never touch files outside the upload dir
        raise ValueError("Resume path escapes upload dir")
    return path


def store_resume(db: Session, candidate: Candidate, filename: str, data: bytes) -> str:
    """Save the original file, extract text, and record parse status on the candidate.

    Returns the previous resume path (if any). The caller deletes it after commit.
    """
    s = get_settings()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise UploadError(f"Resume is larger than {s.max_upload_mb} MB.")
    kind = sniff_type(filename, data)
    if kind is None:
        raise UploadError("Resume must be a PDF or DOCX file.")

    s.upload_dir.mkdir(parents=True, exist_ok=True)
    rel = f"resumes/{uuid.uuid4().hex}.{kind}"
    path = _resume_abspath(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    old = candidate.resume_path
    result = parse_resume(filename, data)
    candidate.resume_path = rel
    candidate.resume_filename = Path(filename).name[:300]
    candidate.resume_text = result.text
    candidate.resume_parse_status = result.status.value
    candidate.resume_parse_warnings = "\n".join(result.warnings)

    if candidate.id:
        log_activity(
            db,
            candidate.id,
            "resume_uploaded",
            f"Resume uploaded ({result.status.value}, {len(result.text)} chars extracted)",
            actor="recruiter",
            detail={"status": result.status.value, "warnings": result.warnings},
        )
    return old


def delete_resume_file(rel: str) -> None:
    if not rel:
        return
    try:
        _resume_abspath(rel).unlink(missing_ok=True)
    except Exception as exc:
        log.error("Could not delete resume file: %s", type(exc).__name__)


def resume_file_path(candidate: Candidate) -> Path | None:
    if not candidate.resume_path:
        return None
    path = _resume_abspath(candidate.resume_path)
    return path if path.exists() else None


def delete_candidate(db: Session, candidate: Candidate) -> None:
    """Remove the candidate, every linked row (cascades) and the resume file."""
    rel = candidate.resume_path
    db.delete(candidate)
    db.commit()
    delete_resume_file(rel)


def search_candidates(db: Session, q: str = "") -> list[Candidate]:
    stmt = select(Candidate).order_by(Candidate.created_at.desc())
    q = q.strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Candidate.full_name.ilike(like),
                Candidate.email.ilike(like),
                Candidate.phone.ilike(like),
                Candidate.summary_notes.ilike(like),
                Candidate.resume_text.ilike(like),
            )
        )
    return list(db.scalars(stmt))


def parse_status_label(status: str) -> str:
    return {
        ParseStatus.ok.value: "Parsed",
        ParseStatus.partial.value: "Partially parsed",
        ParseStatus.failed.value: "Parsing failed",
        ParseStatus.none.value: "No resume",
    }.get(status, status)
