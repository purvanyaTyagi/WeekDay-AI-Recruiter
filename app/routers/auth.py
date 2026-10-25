from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import csrf_protect, verify_credentials
from app.templating import templates

router = APIRouter()


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/candidates"


@router.get("/login")
async def login_form(request: Request, next: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None})


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/candidates"),
):
    if not verify_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": "Invalid username or password."},
            status_code=401,
        )
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session["user"] = username
    if csrf:
        request.session["csrf"] = csrf
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
