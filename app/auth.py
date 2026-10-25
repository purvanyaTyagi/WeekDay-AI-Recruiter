"""Single-recruiter auth: bcrypt hash from env, signed session cookie, CSRF token."""

import hmac
import secrets

import bcrypt
from fastapi import Request

from app.config import get_settings


class LoginRequired(Exception):
    pass


class CSRFError(Exception):
    pass


def verify_credentials(username: str, password: str) -> bool:
    s = get_settings()
    if not s.recruiter_password_hash:
        return False
    user_ok = hmac.compare_digest(username.encode(), s.recruiter_username.encode())
    try:
        pw_ok = bcrypt.checkpw(password.encode(), s.recruiter_password_hash.encode())
    except ValueError:
        return False
    return user_ok and pw_ok


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()  # FastAPI caches the parsed form for Form() params
        sent = form.get("csrf_token")
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise CSRFError()
