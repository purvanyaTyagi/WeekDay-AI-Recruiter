"""Test setup: isolated SQLite DB + upload dir, migrations applied, no real network."""

import io
import os
import re
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="recruiter-tests-"))
os.environ.update(
    {
        # Set TEST_DATABASE_URL to run the suite against PostgreSQL (the database is wiped).
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}",
        "UPLOAD_DIR": str(_TMP / "uploads"),
        "SECRET_KEY": "test-secret",
        "RECRUITER_USERNAME": "recruiter",
        # bcrypt hash of "testpass123"
        "RECRUITER_PASSWORD_HASH": "$2b$12$E.2Sj2u9TcjtHDAgM0x6/.q3VT6wYeS/HSm37043dJc.Eei.YkkRi",
        "OPENROUTER_API_KEY": "test-key",
        "OUTREACH_MODEL": "test/outreach",
        "INTERVIEW_MODEL": "test/interview",
        "RUN_SCHEDULER": "false",
        "SMTP_HOST": "",
        "IMAP_HOST": "",
        "TWILIO_ACCOUNT_SID": "",
        "GNANI_API_KEY": "",
        "APP_TIMEZONE": "UTC",
        "BASE_URL": "http://testserver",
    }
)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    command.downgrade(cfg, "base")  # start clean when pointed at a reused database
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def upload_dir() -> Path:
    return Path(os.environ["UPLOAD_DIR"])


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found in page"
    return m.group(1)


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    page = client.get("/login")
    token = csrf_from(page.text)
    r = client.post(
        "/login",
        data={"username": "recruiter", "password": "testpass123", "csrf_token": token, "next": "/candidates"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.csrf = token
    client.headers["X-CSRF-Token"] = token
    return client


# ---------- sample files ----------


def make_pdf(text_pages: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for page in text_pages:
        y = 800
        for line in page.splitlines():
            c.drawString(50, y, line)
            y -= 14
        c.showPage()
    c.save()
    return buf.getvalue()


def make_docx(paragraphs: list[str]) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Skill"
    t.rows[0].cells[1].text = "Python"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


RESUME_LINES = "\n".join(
    [
        "Jane Doe - Senior Backend Engineer",
        "Experience: 6 years building Python services with FastAPI and Django.",
        "Led migration of a monolith to event-driven microservices on AWS.",
        "Skills: Python, PostgreSQL, Kafka, Docker, Kubernetes, Terraform.",
        "Education: BSc Computer Science.",
        "Projects: open-source contributor to SQLAlchemy plugins.",
    ]
)


# ---------- fakes for external services ----------


@pytest.fixture(autouse=True)
def fakes():
    """Every test gets a fake model and fresh console adapters; quiet hours off by default."""
    from app.adapters import set_adapters
    from app.adapters.email.console import ConsoleEmailAdapter, NullInboxAdapter
    from app.adapters.sms.console import ConsoleSMSAdapter
    from app.services.llm import set_llm
    from app.services.speech import set_speech
    from tests.fakes import FakeLLM, FakeSpeech

    from app.interview import session as interview_session
    from app.interview import summary

    summary.RUN_INLINE = True
    interview_session._locks.clear()
    interview_session._pending.clear()
    llm = FakeLLM()
    email, sms = ConsoleEmailAdapter(), ConsoleSMSAdapter()
    speech = FakeSpeech()
    set_llm(llm)
    set_speech(speech)
    set_adapters(email=email, sms=sms, inbox=NullInboxAdapter())

    class F:
        pass

    f = F()
    f.llm, f.email, f.sms, f.speech = llm, email, sms, speech
    yield f
    set_llm(None)
    set_speech(None)
    set_adapters()


def set_runtime(db, **values):
    """Set runtime settings rows (send_mode, quiet_hours_start/end, max_per_day)."""
    from app.models import AppSetting

    for k, v in values.items():
        row = db.get(AppSetting, k)
        if row:
            row.value = str(v)
        else:
            db.add(AppSetting(key=k, value=str(v)))
    db.commit()


@pytest.fixture
def quiet_off(db):
    set_runtime(db, quiet_hours_start="00:00", quiet_hours_end="00:00")


@pytest.fixture
def candidate_job(db, quiet_off):
    from app.models import Candidate, Job

    job = Job(title="Backend Engineer", description="Build Python APIs with FastAPI and PostgreSQL.",
              required_skills="Python, FastAPI, PostgreSQL")
    c = Candidate(full_name="Jane Doe", email="jane@example.com", phone="+14155550123",
                  resume_text=RESUME_LINES, resume_parse_status="ok", timezone="Asia/Kolkata")
    db.add_all([job, c])
    db.commit()
    return c, job
