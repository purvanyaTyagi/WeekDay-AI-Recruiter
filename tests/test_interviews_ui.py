from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Engagement, Interview, InterviewEvaluation, TranscriptEntry, utcnow

SUMMARY = {"summary": "Clear answers on Python.", "strengths": [{"point": "Python", "evidence": "I use asyncio"}],
           "concerns": [], "suggested_score": 3, "reasoning": "Mixed evidence."}


def _iv(db, c, job, status="scheduled", when=None, token="t1" + "x" * 41):
    eng = db.scalar(select(Engagement).where(Engagement.candidate_id == c.id)) or Engagement(
        candidate_id=c.id, job_id=job.id, channel="email")
    db.add(eng)
    db.flush()
    iv = Interview(candidate_id=c.id, job_id=job.id, engagement_id=eng.id, token=token, status=status,
                   scheduled_at=when or utcnow() + timedelta(days=1), briefing="Probe Kafka.", channel="email")
    db.add(iv)
    db.commit()
    return iv


def test_list_and_filters(auth_client, db, candidate_job):
    c, job = candidate_job
    a = _iv(db, c, job, when=utcnow() + timedelta(days=1), token="a" * 43)
    b = _iv(db, c, job, status="completed", when=utcnow() - timedelta(days=5), token="b" * 43)
    page = auth_client.get("/interviews").text
    assert "Jane Doe" in page and "Probe Kafka." in page and "Copy link" in page
    assert f"http://testserver/i/{a.token}" in page
    only_done = auth_client.get("/interviews?status=completed").text
    assert f"/interviews/{b.id}\"" in only_done and f"/interviews/{a.id}\"" not in only_done
    future = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    dated = auth_client.get(f"/interviews?date_from={future}&date_to={future}").text
    assert f"/interviews/{a.id}\"" in dated and f"/interviews/{b.id}\"" not in dated


def test_detail_shows_transcript_and_evaluation(auth_client, db, candidate_job):
    c, job = candidate_job
    iv = _iv(db, c, job, status="completed", when=utcnow() - timedelta(hours=1))
    db.add_all([TranscriptEntry(interview_id=iv.id, seq=1, role="agent", content="What do you use asyncio for?"),
                TranscriptEntry(interview_id=iv.id, seq=2, role="candidate", content="I use asyncio daily."),
                InterviewEvaluation(interview_id=iv.id, summary="Clear answers.", suggested_score=4,
                                    strengths=[{"point": "Async", "evidence": "I use asyncio daily."}],
                                    concerns=[], reasoning="Specific.")])
    db.commit()
    page = auth_client.get(f"/interviews/{iv.id}").text
    assert "I use asyncio daily." in page and "AI interviewer" in page
    assert "recommendation only" in page.lower() and "suggested score" in page


def test_cancel_notifies_candidate(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    iv = _iv(db, c, job)
    r = auth_client.post(f"/interviews/{iv.id}/cancel", data={"notify": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Interview, iv.id).status == "cancelled"
    assert "cancelled" in fakes.email.outbox[-1].body
    assert "has been cancelled" in auth_client.get(f"/i/{iv.token}").text


def test_reschedule_no_show_resends_link(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    iv = _iv(db, c, job, status="no_show", when=utcnow() - timedelta(hours=2))
    new = (utcnow() + timedelta(days=2)).astimezone(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0)
    r = auth_client.post(f"/interviews/{iv.id}/reschedule", data={"start_local": new.strftime("%Y-%m-%dT%H:%M")},
                         follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    iv = db.get(Interview, iv.id)
    assert iv.status == "scheduled" and iv.scheduled_at == new.astimezone(ZoneInfo("UTC"))
    body = fakes.email.outbox[-1].body
    assert "rescheduled" in body and iv.token in body and new.strftime("%H:%M") in body


def test_end_early_marks_incomplete_and_summarises(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    iv = _iv(db, c, job, status="in_progress", when=utcnow())
    db.add(TranscriptEntry(interview_id=iv.id, seq=1, role="candidate", content="I use asyncio"))
    db.commit()
    fakes.llm.queue(SUMMARY)
    auth_client.post(f"/interviews/{iv.id}/end")
    db.expire_all()
    iv = db.get(Interview, iv.id)
    assert iv.status == "incomplete" and "recruiter" in iv.end_reason
    assert iv.evaluation.suggested_score == 3


def test_regenerate_summary_after_failure(auth_client, db, candidate_job, fakes):
    c, job = candidate_job
    iv = _iv(db, c, job, status="completed", when=utcnow() - timedelta(hours=1))
    db.add(TranscriptEntry(interview_id=iv.id, seq=1, role="candidate", content="An answer"))
    db.commit()
    fakes.llm.queue("not json", "still not json", SUMMARY)
    auth_client.post(f"/interviews/{iv.id}/summary")
    db.expire_all()
    assert db.get(Interview, iv.id).evaluation.error
    assert "Summary failed" in auth_client.get(f"/interviews/{iv.id}").text
    auth_client.post(f"/interviews/{iv.id}/summary")
    db.expire_all()
    ev = db.get(Interview, iv.id).evaluation
    assert ev.error == "" and ev.suggested_score == 3


def test_controls_reject_wrong_states(auth_client, db, candidate_job):
    c, job = candidate_job
    iv = _iv(db, c, job, status="completed", when=utcnow() - timedelta(hours=1))
    auth_client.post(f"/interviews/{iv.id}/cancel")
    auth_client.post(f"/interviews/{iv.id}/end")
    db.expire_all()
    assert db.get(Interview, iv.id).status == "completed"
