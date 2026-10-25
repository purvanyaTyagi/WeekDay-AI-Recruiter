
from sqlalchemy import func, select

from app.models import (
    ActivityLog,
    Candidate,
    Engagement,
    Interview,
    InterviewEvaluation,
    Job,
    Message,
    TranscriptEntry,
    utcnow,
)
from tests.conftest import RESUME_LINES, make_docx, make_pdf


def _add(client, files=None, **fields):
    data = {"full_name": "Jane Doe", "email": "jane@example.com", "phone": "+14155550123",
            "preferred_channel": "email", "summary_notes": "Strong backend profile"}
    data.update(fields)
    return client.post("/candidates", data=data, files=files or {}, follow_redirects=False)


def test_requires_login(client):
    r = client.get("/candidates", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_bad_password(client):
    from tests.conftest import csrf_from

    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"username": "recruiter", "password": "nope", "csrf_token": token})
    assert r.status_code == 401


def test_post_without_csrf_rejected(auth_client):
    del auth_client.headers["X-CSRF-Token"]
    r = auth_client.post("/candidates", data={"full_name": "X", "email": "x@example.com"})
    assert r.status_code == 403


def test_create_with_pdf_resume(auth_client, db, upload_dir):
    r = _add(auth_client, files={"resume": ("cv.pdf", make_pdf([RESUME_LINES]), "application/pdf")})
    assert r.status_code == 303, r.text
    c = db.scalar(select(Candidate))
    assert c.resume_parse_status == "ok"
    assert "Kafka" in c.resume_text
    assert (upload_dir / c.resume_path).exists()
    page = auth_client.get(f"/candidates/{c.id}")
    assert "Jane Doe" in page.text and "Parsed" in page.text
    assert auth_client.get(f"/candidates/{c.id}/resume").content[:5] == b"%PDF-"
    kinds = {a.kind for a in c.activities}
    assert {"candidate_created", "resume_uploaded"} <= kinds


def test_failed_parse_is_flagged_in_ui(auth_client, db):
    _add(auth_client, files={"resume": ("scan.pdf", make_pdf([""]), "application/pdf")})
    c = db.scalar(select(Candidate))
    assert c.resume_parse_status == "failed"
    page = auth_client.get(f"/candidates/{c.id}").text
    assert "Parsing failed" in page and "Parsing problem" in page


def test_validation_errors(auth_client):
    r = _add(auth_client, full_name="", email="bad", phone="123")
    assert r.status_code == 422
    assert "Full name is required" in r.text
    assert "valid email" in r.text
    assert "international format" in r.text


def test_rejects_non_resume_file(auth_client, db):
    r = _add(auth_client, files={"resume": ("cv.pdf", b"hello", "application/pdf")})
    assert r.status_code == 422
    assert db.scalar(select(func.count()).select_from(Candidate)) == 0


def test_search(auth_client):
    _add(auth_client, full_name="Jane Doe", email="jane@example.com")
    _add(auth_client, full_name="Raj Kumar", email="raj@example.com", phone="+919800000000")
    auth_client.get("/candidates")  # consume "Added ..." flash messages
    r = auth_client.get("/candidates?q=raj")
    assert "Raj Kumar" in r.text and "Jane Doe" not in r.text
    partial = auth_client.get("/candidates?q=jane", headers={"HX-Request": "true", "HX-Target": "candidate-rows"})
    assert "<html" not in partial.text and "Jane Doe" in partial.text


def test_edit_replaces_resume_and_removes_old_file(auth_client, db, upload_dir):
    _add(auth_client, files={"resume": ("cv.pdf", make_pdf([RESUME_LINES]), "application/pdf")})
    c = db.scalar(select(Candidate))
    old = upload_dir / c.resume_path
    r = auth_client.post(
        f"/candidates/{c.id}/edit",
        data={"full_name": "Jane Q. Doe", "email": "jane@example.com", "phone": "+14155550123",
              "preferred_channel": "sms", "timezone": "Asia/Kolkata"},
        files={"resume": ("cv.docx", make_docx(RESUME_LINES.splitlines()), "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    db.expire_all()
    c = db.get(Candidate, c.id)
    assert c.full_name == "Jane Q. Doe" and c.preferred_channel == "sms" and c.timezone == "Asia/Kolkata"
    assert c.resume_path.endswith(".docx")
    assert not old.exists() and (upload_dir / c.resume_path).exists()


def test_delete_removes_everything(auth_client, db, upload_dir):
    _add(auth_client, files={"resume": ("cv.pdf", make_pdf([RESUME_LINES]), "application/pdf")})
    c = db.scalar(select(Candidate))
    resume = upload_dir / c.resume_path
    job = Job(title="Backend Engineer", description="Build APIs")
    db.add(job)
    db.flush()
    eng = Engagement(candidate_id=c.id, job_id=job.id)
    db.add(eng)
    db.flush()
    db.add(Message(candidate_id=c.id, engagement_id=eng.id, direction="outbound", channel="email",
                   body="hi", status="sent"))
    iv = Interview(candidate_id=c.id, job_id=job.id, engagement_id=eng.id, token="tok", scheduled_at=utcnow())
    db.add(iv)
    db.flush()
    db.add(TranscriptEntry(interview_id=iv.id, seq=1, role="agent", content="Hello"))
    db.add(InterviewEvaluation(interview_id=iv.id, summary="ok"))
    db.commit()

    r = auth_client.post(f"/candidates/{c.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    for model in (Candidate, Engagement, Message, Interview, TranscriptEntry, InterviewEvaluation, ActivityLog):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__
    assert not resume.exists()
    assert db.scalar(select(func.count()).select_from(Job)) == 1  # jobs are not candidate data


def test_jobs_crud(auth_client, db):
    r = auth_client.post("/jobs", data={"title": "", "description": ""})
    assert r.status_code == 422
    r = auth_client.post("/jobs", data={"title": "Backend Engineer", "description": "Python APIs",
                                         "required_skills": "Python, SQL"}, follow_redirects=False)
    assert r.status_code == 303
    job = db.scalar(select(Job))
    r = auth_client.post(f"/jobs/{job.id}/edit", data={"title": "Senior Backend Engineer",
                                                      "description": "Python APIs"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Senior Backend Engineer" in auth_client.get("/jobs").text
