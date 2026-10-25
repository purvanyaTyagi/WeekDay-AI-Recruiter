"""Outreach safety rails: send mode, quiet hours, daily limit, opt-out detection."""

import re
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Candidate, Direction, Message, MessageStatus, utcnow
from app.services import app_settings
from app.templating import tz_or_default

# Whole-message replies that mean "stop" (carrier-style keywords).
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt out", "remove"}
# Phrases anywhere in the message. Deliberately broad: a false positive only pauses
# the agent and flags the candidate for a human, which is the safe direction.
STOP_PHRASES = [
    r"\bunsubscribe\b",
    r"\bnot interested\b",
    r"\bno longer interested\b",
    r"\bremove me\b",
    r"\btake me off\b",
    r"\bdo not (contact|message|email|text)\b",
    r"\bdon'?t (contact|message|email|text)\b",
    r"\bstop (contacting|messaging|emailing|texting|sending)\b",
    r"\bopt[\s-]?out\b",
    r"\bleave me alone\b",
]
_STOP_RE = re.compile("|".join(STOP_PHRASES), re.IGNORECASE)


def is_opt_out(text: str) -> bool:
    # Only look at the new part of an email reply, not the quoted thread below it.
    body = strip_quoted(text)
    norm = re.sub(r"[^a-z ]", "", body.lower()).strip()
    if norm in STOP_WORDS:
        return True
    return bool(_STOP_RE.search(body))


def strip_quoted(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith(">"):
            continue
        if re.match(r"^On .+wrote:\s*$", line.strip()):
            break
        if line.strip() in ("-----Original Message-----",):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def effective_send_mode(db: Session, candidate: Candidate) -> str:
    return candidate.send_mode_override or app_settings.load(db).send_mode


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def quiet_hours_until(db: Session, candidate: Candidate, now: datetime | None = None) -> datetime | None:
    """If it is currently quiet hours for the candidate, return when they end (UTC); else None."""
    rs = app_settings.load(db)
    start, end = _parse_hhmm(rs.quiet_hours_start), _parse_hhmm(rs.quiet_hours_end)
    if start == end:
        return None  # quiet hours disabled
    tz = tz_or_default(candidate.timezone)
    local = (now or utcnow()).astimezone(tz)
    t = local.time()
    in_quiet = (start <= t < end) if start < end else (t >= start or t < end)
    if not in_quiet:
        return None
    end_local = datetime.combine(local.date(), end, tzinfo=tz)
    if end_local <= local:
        end_local += timedelta(days=1)
    return end_local.astimezone(utcnow().tzinfo)


def sent_last_24h(db: Session, candidate_id: int, now: datetime | None = None) -> int:
    since = (now or utcnow()) - timedelta(hours=24)
    return db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.candidate_id == candidate_id,
            Message.direction == Direction.outbound.value,
            Message.status.in_([MessageStatus.sent.value, MessageStatus.queued.value]),
            Message.created_at >= since,
        )
    ) or 0


def over_daily_limit(db: Session, candidate_id: int) -> bool:
    return sent_last_24h(db, candidate_id) >= app_settings.load(db).max_per_day
