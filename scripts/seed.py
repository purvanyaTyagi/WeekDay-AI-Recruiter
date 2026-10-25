"""Load sample data: one job and three candidates (one with a DOCX resume).

Usage: python -m scripts.seed
Safe to run twice: it does nothing if the sample job already exists.
Uses example.com addresses and +1-555 numbers, which can't reach real people.
"""

import io

import docx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Candidate, Job
from app.services.activity import log_activity
from app.services.candidates import store_resume

JOB_TITLE = "Senior Backend Engineer (Python)"
JOB_DESCRIPTION = """We're hiring a Senior Backend Engineer to build and scale the APIs behind our hiring platform.

What you'll do
- Design, build and operate Python services (FastAPI) used by thousands of recruiters.
- Model data in PostgreSQL and keep queries fast as we grow.
- Build event-driven pipelines (Kafka or similar) for notifications and analytics.
- Own reliability: monitoring, on-call rotation, incident reviews.
- Mentor other engineers and review designs.

What we're looking for
- 5+ years building backend systems in Python.
- Strong SQL and PostgreSQL experience, including schema design and query tuning.
- Experience with cloud infrastructure (AWS or GCP), containers and CI/CD.
- Clear written communication; we work remotely across time zones.

Nice to have
- Kafka, Terraform, Kubernetes.
- Experience with LLM-based products.

Remote-friendly. Salary depends on location and experience; the hiring team shares ranges after the first interview."""

CANDIDATES = [
    {
        "full_name": "Asha Verma",
        "email": "asha.verma@example.com",
        "phone": "+15555550101",
        "preferred_channel": "email",
        "timezone": "Asia/Kolkata",
        "summary_notes": "Referred by a former colleague. Strong backend background; currently at a fintech.",
        "linkedin_url": "https://www.linkedin.com/in/example-asha",
        "github_url": "https://github.com/example-asha",
        "resume_docx": [
            "Asha Verma — Backend Engineer",
            "Summary: 7 years building Python services for payments and lending products.",
            "Experience",
            "FinPay (2021–present) Senior Software Engineer: led the move from a Django monolith to FastAPI "
            "services; designed the ledger schema in PostgreSQL; built Kafka consumers for payment events.",
            "LendCo (2017–2021) Software Engineer: built loan-origination APIs, cut p95 latency by 40% through "
            "query tuning and caching with Redis.",
            "Skills: Python, FastAPI, Django, PostgreSQL, Kafka, Redis, AWS (ECS, RDS), Terraform, Docker.",
            "Education: B.Tech in Computer Science.",
        ],
    },
    {
        "full_name": "Marcus Lee",
        "email": "marcus.lee@example.com",
        "phone": "+15555550102",
        "preferred_channel": "sms",
        "timezone": "America/New_York",
        "summary_notes": "Applied through the careers page. Prefers text messages.",
        "github_url": "https://github.com/example-marcus",
        "resume_text": (
            "Marcus Lee\nSoftware Engineer, 5 years.\n"
            "Acme Logistics (2020–present): Python microservices for shipment tracking on GCP; "
            "PostgreSQL and BigQuery; on-call lead for the tracking platform.\n"
            "Startup XYZ (2019–2020): full-stack engineer, Flask and React.\n"
            "Skills: Python, Flask, FastAPI, PostgreSQL, GCP, Kubernetes, Pub/Sub."
        ),
    },
    {
        "full_name": "Sofia Martins",
        "email": "sofia.martins@example.com",
        "phone": "",
        "preferred_channel": "email",
        "timezone": "Europe/Lisbon",
        "summary_notes": "Sourced from a conference talk on event-driven systems. No resume yet.",
        "portfolio_url": "https://example.com/sofia",
    },
]


def _docx(lines: list[str]) -> bytes:
    d = docx.Document()
    for line in lines:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def main() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(Job).where(Job.title == JOB_TITLE)):
            print("Sample data already present; nothing to do.")
            return
        db.add(Job(title=JOB_TITLE, description=JOB_DESCRIPTION,
                   required_skills="Python, FastAPI, PostgreSQL, AWS or GCP, Kafka (nice to have)"))
        for data in CANDIDATES:
            data = dict(data)
            lines = data.pop("resume_docx", None)
            text = data.pop("resume_text", None)
            c = Candidate(**data)
            if text:
                c.resume_text, c.resume_parse_status = text, "ok"
            db.add(c)
            db.flush()
            if lines:
                store_resume(db, c, f"{c.full_name.replace(' ', '_')}_resume.docx", _docx(lines))
            log_activity(db, c.id, "candidate_created", "Candidate added by seed script", actor="system")
        db.commit()
        print(f"Seeded 1 job and {len(CANDIDATES)} candidates.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
