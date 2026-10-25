from sqlalchemy import func, select

from app.models import Candidate, Job


def test_seed_is_idempotent(db, upload_dir):
    from scripts import seed

    seed.main()
    seed.main()
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    cands = {c.full_name: c for c in db.scalars(select(Candidate))}
    assert len(cands) == 3
    asha = cands["Asha Verma"]
    assert asha.resume_parse_status == "ok" and "Kafka" in asha.resume_text
    assert (upload_dir / asha.resume_path).exists()
