"""Per-candidate activity log. Keep `detail` small: never put full resumes,
full messages or transcripts in here."""

from sqlalchemy.orm import Session

from app.models import ActivityLog


def log_activity(
    db: Session,
    candidate_id: int,
    kind: str,
    summary: str,
    actor: str = "system",
    detail: dict | None = None,
) -> ActivityLog:
    entry = ActivityLog(
        candidate_id=candidate_id,
        kind=kind,
        actor=actor,
        summary=summary[:500],
        detail=detail or {},
    )
    db.add(entry)
    return entry
