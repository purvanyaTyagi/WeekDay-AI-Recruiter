import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import CSRFError, LoginRequired
from app.config import BASE_DIR, get_settings
from app.routers import auth as auth_router
from app.routers import candidates, interview_public, interviews, jobs, outreach, webhooks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    if s.secret_key == "change-me-in-production":
        log.warning("SECRET_KEY is the default value; set it in .env")
    if not s.recruiter_password_hash:
        log.warning("RECRUITER_PASSWORD_HASH is not set; login is disabled. Run scripts/hash_password.py")
    if not s.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY is not set; AI features will fail until it is.")
    if s.run_scheduler:
        from app import scheduler

        scheduler.start()
    yield
    if s.run_scheduler:
        from app import scheduler

        scheduler.shutdown()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="recruiter_session",
        same_site="lax",
        https_only=s.session_https_only,
        max_age=60 * 60 * 12,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, exc: CSRFError):
        return HTMLResponse("Invalid or missing CSRF token. Reload the page and try again.", status_code=403)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def index():
        return RedirectResponse("/candidates", status_code=303)

    app.include_router(auth_router.router)
    app.include_router(candidates.router)
    app.include_router(jobs.router)
    app.include_router(outreach.router)
    app.include_router(webhooks.router)
    app.include_router(interviews.router)
    app.include_router(interview_public.router)
    return app


app = create_app()
