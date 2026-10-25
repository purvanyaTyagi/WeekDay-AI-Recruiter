"""Background jobs (APScheduler, in-process).

Every job re-reads state from the database, so nothing is lost on restart and
no job store is needed. Run the scheduler in exactly one process
(RUN_SCHEDULER=true on one instance only).
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db import SessionLocal

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run(name: str, fn) -> None:
    db = SessionLocal()
    try:
        fn(db)
    except Exception:
        db.rollback()
        log.exception("Scheduled job %s failed", name)
    finally:
        db.close()


def job_flush_queue() -> None:
    from app.services.messaging import flush_queued

    _run("flush_queue", flush_queued)


def job_poll_inbox() -> None:
    from app.services.inbound import poll_inbox

    if get_settings().imap_configured:
        _run("poll_inbox", poll_inbox)


def job_reminders() -> None:
    from app.services.handoff import send_due_reminders

    _run("reminders", send_due_reminders)


def job_check_interviews() -> None:
    from app.interview.monitor import check_interviews

    _run("check_interviews", check_interviews)


JOBS: list[tuple] = [
    # (function, seconds)
    (job_flush_queue, 60),
    (job_poll_inbox, 60),  # interval comes from IMAP_POLL_SECONDS
    (job_reminders, 60),
    (job_check_interviews, 60),
]


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    s = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    for fn, seconds in JOBS:
        interval = s.imap_poll_seconds if fn.__name__ == "job_poll_inbox" else seconds
        _scheduler.add_job(fn, "interval", seconds=interval, id=fn.__name__, replace_existing=True)
    _scheduler.start()
    log.info("Scheduler started with %d jobs", len(JOBS))


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
