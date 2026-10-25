from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import csrf_protect, require_login
from app.db import get_db
from app.models import Engagement, Interview, Job
from app.templating import flash, templates

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_login), Depends(csrf_protect)])


def _get(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _validate(title: str, description: str) -> dict[str, str]:
    errors = {}
    if not title.strip():
        errors["title"] = "Title is required."
    if not description.strip():
        errors["description"] = "Job description is required."
    return errors


@router.get("")
async def list_jobs(request: Request, db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc())))
    counts = dict(
        db.execute(select(Engagement.job_id, func.count()).group_by(Engagement.job_id)).all()
    )
    interview_counts = dict(
        db.execute(select(Interview.job_id, func.count()).group_by(Interview.job_id)).all()
    )
    return templates.TemplateResponse(
        request,
        "jobs/list.html",
        {"jobs": jobs, "counts": counts, "interview_counts": interview_counts},
    )


@router.get("/new")
async def new_job(request: Request):
    return templates.TemplateResponse(request, "jobs/form.html", {"job": None, "values": {}, "errors": {}})


@router.post("")
async def create_job(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    required_skills: str = Form(""),
    db: Session = Depends(get_db),
):
    values = {"title": title, "description": description, "required_skills": required_skills}
    errors = _validate(title, description)
    if errors:
        return templates.TemplateResponse(
            request, "jobs/form.html", {"job": None, "values": values, "errors": errors}, status_code=422
        )
    job = Job(title=title.strip(), description=description.strip(), required_skills=required_skills.strip())
    db.add(job)
    db.commit()
    flash(request, f"Created job “{job.title}”.", "success")
    return RedirectResponse("/jobs", status_code=303)


@router.get("/{job_id}/edit")
async def edit_job_form(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    values = {"title": job.title, "description": job.description, "required_skills": job.required_skills}
    return templates.TemplateResponse(request, "jobs/form.html", {"job": job, "values": values, "errors": {}})


@router.post("/{job_id}/edit")
async def update_job(
    request: Request,
    job_id: int,
    title: str = Form(""),
    description: str = Form(""),
    required_skills: str = Form(""),
    db: Session = Depends(get_db),
):
    job = _get(db, job_id)
    values = {"title": title, "description": description, "required_skills": required_skills}
    errors = _validate(title, description)
    if errors:
        return templates.TemplateResponse(
            request, "jobs/form.html", {"job": job, "values": values, "errors": errors}, status_code=422
        )
    job.title, job.description, job.required_skills = title.strip(), description.strip(), required_skills.strip()
    db.commit()
    flash(request, "Job updated.", "success")
    return RedirectResponse("/jobs", status_code=303)
