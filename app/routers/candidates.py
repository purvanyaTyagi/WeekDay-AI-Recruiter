import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import get_email_adapter, get_sms_adapter
from app.auth import csrf_protect, require_login
from app.db import get_db
from app.models import Candidate, Channel, Job
from app.services import app_settings, safety
from app.services.activity import log_activity
from app.services.candidates import (
    UploadError,
    delete_candidate,
    delete_resume_file,
    parse_status_label,
    resume_file_path,
    search_candidates,
    store_resume,
)
from app.templating import flash, templates

router = APIRouter(prefix="/candidates", dependencies=[Depends(require_login), Depends(csrf_protect)])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")

TEXT_FIELDS = ["full_name", "email", "phone", "summary_notes", "linkedin_url", "github_url", "portfolio_url"]


def _get(db: Session, candidate_id: int) -> Candidate:
    c = db.get(Candidate, candidate_id)
    if not c:
        raise HTTPException(404, "Candidate not found")
    return c


def _parse_form(form) -> tuple[dict, dict[str, str]]:
    data = {f: (form.get(f) or "").strip() for f in TEXT_FIELDS}
    data["phone"] = re.sub(r"[\s\-().]", "", data["phone"])
    data["other_links"] = [
        line.strip() for line in (form.get("other_links") or "").splitlines() if line.strip()
    ]
    data["preferred_channel"] = form.get("preferred_channel") or Channel.email.value
    data["timezone"] = (form.get("timezone") or "").strip()

    errors: dict[str, str] = {}
    if not data["full_name"]:
        errors["full_name"] = "Full name is required."
    if data["email"] and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Enter a valid email address."
    if data["phone"] and not PHONE_RE.match(data["phone"]):
        errors["phone"] = "Use international format, e.g. +14155550123."
    if not data["email"] and not data["phone"]:
        errors["email"] = "Provide an email or a mobile number."
    if data["preferred_channel"] not in (Channel.email.value, Channel.sms.value):
        errors["preferred_channel"] = "Invalid channel."
    elif data["preferred_channel"] == Channel.sms.value and not data["phone"]:
        errors["preferred_channel"] = "SMS needs a mobile number."
    elif data["preferred_channel"] == Channel.email.value and not data["email"]:
        errors["preferred_channel"] = "Email needs an email address."
    if data["timezone"]:
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            errors["timezone"] = "Unknown timezone. Use an IANA name like Asia/Kolkata."
    for f in ("linkedin_url", "github_url", "portfolio_url"):
        if data[f] and not data[f].startswith(("http://", "https://")):
            data[f] = "https://" + data[f]
    return data, errors


async def _read_upload(upload: UploadFile | None) -> tuple[str, bytes] | None:
    if upload is None or not getattr(upload, "filename", ""):
        return None
    data = await upload.read()
    if not data:
        return None
    return upload.filename, data


@router.get("")
async def list_candidates(request: Request, q: str = "", db: Session = Depends(get_db)):
    ctx = {"candidates": search_candidates(db, q), "q": q, "parse_label": parse_status_label}
    if request.headers.get("hx-request") and request.headers.get("hx-target") == "candidate-rows":
        return templates.TemplateResponse(request, "partials/candidate_rows.html", ctx)
    return templates.TemplateResponse(request, "candidates/list.html", ctx)


@router.get("/new")
async def new_candidate(request: Request):
    return templates.TemplateResponse(
        request, "candidates/form.html", {"candidate": None, "values": {}, "errors": {}}
    )


@router.post("")
async def create_candidate(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    data, errors = _parse_form(form)
    upload = await _read_upload(form.get("resume"))
    candidate = Candidate(**data)
    if not errors and upload:
        try:
            store_resume(db, candidate, *upload)
        except UploadError as e:
            errors["resume"] = str(e)
    if errors:
        return templates.TemplateResponse(
            request,
            "candidates/form.html",
            {"candidate": None, "values": data, "errors": errors},
            status_code=422,
        )
    db.add(candidate)
    db.flush()
    log_activity(db, candidate.id, "candidate_created", "Candidate added", actor="recruiter")
    if upload:
        log_activity(
            db,
            candidate.id,
            "resume_uploaded",
            f"Resume uploaded ({candidate.resume_parse_status}, {len(candidate.resume_text)} chars extracted)",
            actor="recruiter",
        )
    db.commit()
    flash(request, f"Added {candidate.full_name}.", "success")
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.get("/{candidate_id}")
async def candidate_detail(request: Request, candidate_id: int, db: Session = Depends(get_db)):
    c = _get(db, candidate_id)
    jobs = list(db.scalars(select(Job).order_by(Job.title)))
    dev_simulation = get_email_adapter().name == "console" or get_sms_adapter().name == "console"
    return templates.TemplateResponse(
        request,
        "candidates/detail.html",
        {
            "c": c,
            "jobs": jobs,
            "parse_label": parse_status_label,
            "send_mode": safety.effective_send_mode(db, c),
            "global_mode": app_settings.load(db).send_mode,
            "dev_simulation": dev_simulation,
        },
    )


@router.get("/{candidate_id}/edit")
async def edit_candidate_form(request: Request, candidate_id: int, db: Session = Depends(get_db)):
    c = _get(db, candidate_id)
    values = {f: getattr(c, f) for f in TEXT_FIELDS}
    values.update(other_links=c.other_links, preferred_channel=c.preferred_channel, timezone=c.timezone)
    return templates.TemplateResponse(
        request, "candidates/form.html", {"candidate": c, "values": values, "errors": {}}
    )


@router.post("/{candidate_id}/edit")
async def update_candidate(request: Request, candidate_id: int, db: Session = Depends(get_db)):
    c = _get(db, candidate_id)
    form = await request.form()
    data, errors = _parse_form(form)
    upload = await _read_upload(form.get("resume"))
    old_resume = ""
    if not errors:
        for k, v in data.items():
            setattr(c, k, v)
        if upload:
            try:
                old_resume = store_resume(db, c, *upload)
            except UploadError as e:
                errors["resume"] = str(e)
    if errors:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "candidates/form.html",
            {"candidate": c, "values": data, "errors": errors},
            status_code=422,
        )
    log_activity(db, c.id, "candidate_updated", "Candidate details edited", actor="recruiter")
    db.commit()
    delete_resume_file(old_resume)
    flash(request, "Candidate updated.", "success")
    return RedirectResponse(f"/candidates/{c.id}", status_code=303)


@router.post("/{candidate_id}/delete")
async def remove_candidate(request: Request, candidate_id: int, db: Session = Depends(get_db)):
    c = _get(db, candidate_id)
    name = c.full_name
    delete_candidate(db, c)
    flash(request, f"Deleted {name} and all linked data.", "success")
    return RedirectResponse("/candidates", status_code=303)


@router.get("/{candidate_id}/resume")
async def download_resume(candidate_id: int, db: Session = Depends(get_db)):
    c = _get(db, candidate_id)
    path = resume_file_path(c)
    if not path:
        raise HTTPException(404, "No resume on file")
    return FileResponse(path, filename=c.resume_filename or path.name)
