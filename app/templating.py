from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.auth import get_csrf_token
from app.config import BASE_DIR, get_settings


def tz_or_default(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or get_settings().app_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def localdt(value: datetime | None, tz: str | None = None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    if value is None:
        return ""
    return value.astimezone(tz_or_default(tz)).strftime(fmt)


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flashes", []).append({"message": message, "kind": kind})


def _context(request: Request) -> dict:
    flashes = request.session.pop("flashes", []) if "session" in request.scope else []
    return {
        "csrf_token": get_csrf_token(request) if "session" in request.scope else "",
        "settings": get_settings(),
        "flashes": flashes,
        "current_user": request.session.get("user") if "session" in request.scope else None,
    }


templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"), context_processors=[_context])
templates.env.filters["localdt"] = localdt
