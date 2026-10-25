"""Runtime settings the recruiter can change in the UI.

Values live in the app_settings table; .env provides the defaults.
"""

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class RuntimeSettings:
    send_mode: str  # auto | review
    quiet_hours_start: str  # HH:MM
    quiet_hours_end: str
    max_per_day: int


def _get(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def load(db: Session) -> RuntimeSettings:
    env = get_settings()
    max_per_day = _get(db, "max_per_day")
    return RuntimeSettings(
        send_mode=_get(db, "send_mode") or env.default_send_mode,
        quiet_hours_start=_get(db, "quiet_hours_start") or env.quiet_hours_start,
        quiet_hours_end=_get(db, "quiet_hours_end") or env.quiet_hours_end,
        max_per_day=int(max_per_day) if max_per_day else env.max_outbound_per_candidate_per_day,
    )


def save(db: Session, send_mode: str, quiet_start: str, quiet_end: str, max_per_day: int) -> dict[str, str]:
    errors = {}
    if send_mode not in ("auto", "review"):
        errors["send_mode"] = "Choose auto or review."
    if not TIME_RE.match(quiet_start):
        errors["quiet_hours_start"] = "Use HH:MM (24h)."
    if not TIME_RE.match(quiet_end):
        errors["quiet_hours_end"] = "Use HH:MM (24h)."
    if not 1 <= max_per_day <= 50:
        errors["max_per_day"] = "Between 1 and 50."
    if errors:
        return errors
    _set(db, "send_mode", send_mode)
    _set(db, "quiet_hours_start", quiet_start)
    _set(db, "quiet_hours_end", quiet_end)
    _set(db, "max_per_day", str(max_per_day))
    return {}
