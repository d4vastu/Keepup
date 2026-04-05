import asyncio
import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .admin import router as admin_router
from .auth import admin_exists, get_session_secret
from .auth_router import router as auth_router
from .log_buffer import setup_log_buffer
from .notifications import get_unread_count, get_notifications, mark_all_read
from .auto_update_scheduler import apply_all_schedules, scheduler
from .auto_updates_router import router as auto_updates_router
from .backend_loader import get_backends, get_dockerhub_creds, reload_backends
from .config_manager import get_hosts, get_ssh_config
from .credentials import get_credentials, save_sudo_password
from .ssh_client import _needs_sudo, check_host_updates, reboot_host, run_host_update_buffered
from .__version__ import APP_VERSION
from .templates_env import make_templates

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

_PUBLIC_PATHS = {"/", "/login", "/logout", "/setup", "/forgot-password",
                 "/forgot-password/reset"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/setup/"):
            return await call_next(request)
        if not admin_exists():
            return RedirectResponse("/setup", status_code=302)
        if not request.session.get("authenticated"):
            return RedirectResponse(f"/login?next={path}", status_code=302)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

setup_log_buffer()
app = FastAPI(title="Keepup")
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware,
                   secret_key=get_session_secret(),
                   session_cookie="ud_session",
                   max_age=30 * 24 * 3600,   # 30 days max; login sets shorter if no remember_me
                   https_only=False,
                   same_site="lax")
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(auto_updates_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = make_templates()

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_VERSION_FILE = _DATA_DIR / ".app_version"


def _check_version_notification() -> None:
    """On first startup after a new version, create a notification with a release notes link."""
    try:
        stored = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else ""
        if stored != APP_VERSION:
            if stored:  # only notify on upgrade, not on fresh install
                from .notifications import notify
                notify(
                    f"Updated to v{APP_VERSION}",
                    f"Keepup was upgraded from v{stored} to v{APP_VERSION}. "
                    f"See what's new: https://github.com/d4vastu/keepup/releases/tag/v{APP_VERSION}",
                    level="info",
                )
            _VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _VERSION_FILE.write_text(APP_VERSION)
    except Exception:
        pass


@app.on_event("startup")
async def _startup() -> None:
    _check_version_notification()
    await reload_backends()
    apply_all_schedules()
    scheduler.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_host(slug: str) -> dict:
    for h in get_hosts():
        if h["slug"] == slug:
            return h
    raise KeyError(f"Host {slug!r} not in config")


_jobs: dict[str, dict] = {}



# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------

async def _job_run_host_update(job_id: str, host: dict, creds: dict) -> None:
    try:
        lines = await run_host_update_buffered(host, get_ssh_config(), creds)
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_host_restart(job_id: str, host: dict, creds: dict) -> None:
    try:
        lines = await reboot_host(host, get_ssh_config(), creds)
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_stack_update(job_id: str, backend_key: str, ref: str) -> None:
    try:
        backend = next((b for b in get_backends() if b.BACKEND_KEY == backend_key), None)
        if backend is None:
            raise ValueError(f"Backend {backend_key!r} not available")
        await backend.update_stack(ref)
        _jobs[job_id]["lines"] = ["Stack updated — containers restarted with new images."]
        _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["done"] = True


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    if request.session.get("authenticated"):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    hosts = get_hosts()
    backends = get_backends()
    docker_configured = (
        any(b.BACKEND_KEY == "portainer" for b in backends)
        or any(h.get("docker_mode") for h in hosts)
    )
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "hosts": hosts, "docker_configured": docker_configured, "app_version": APP_VERSION},
    )


# ---------------------------------------------------------------------------
# Routes — OS updates
# ---------------------------------------------------------------------------

@app.get("/api/host/{slug}/check", response_class=HTMLResponse)
async def host_check(request: Request, slug: str) -> HTMLResponse:
    try:
        host = _get_host(slug)
        creds = get_credentials(slug)
        result = await check_host_updates(host, get_ssh_config(), creds)
        return templates.TemplateResponse(
            "partials/host_status.html",
            {
                "request": request,
                "slug": slug,
                "packages": result["packages"],
                "reboot_required": result["reboot_required"],
                "package_manager": result.get("package_manager", ""),
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post("/api/host/{slug}/update", response_class=HTMLResponse)
async def host_update(
    request: Request,
    slug: str,
    background_tasks: BackgroundTasks,
    sudo_password: str = Form(""),
    save_sudo: str = Form(""),
) -> HTMLResponse:
    try:
        host = _get_host(slug)
        creds = get_credentials(slug)

        if _needs_sudo(host, get_ssh_config()):
            effective_sudo = sudo_password.strip() or creds.get("sudo_password", "")
            if not effective_sudo:
                return templates.TemplateResponse(
                    "partials/sudo_modal.html",
                    {"request": request, "slug": slug, "action": "update"},
                )
            if sudo_password.strip() and save_sudo == "save":
                save_sudo_password(slug, sudo_password.strip())
            creds = {**creds, "sudo_password": effective_sudo}

        job_id = uuid.uuid4().hex[:8]
        _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
        background_tasks.add_task(_job_run_host_update, job_id, host, creds)
        return templates.TemplateResponse(
            "partials/job_poll.html",
            {"request": request, "job_id": job_id},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post("/api/host/{slug}/restart", response_class=HTMLResponse)
async def host_restart(
    request: Request,
    slug: str,
    background_tasks: BackgroundTasks,
    sudo_password: str = Form(""),
    save_sudo: str = Form(""),
) -> HTMLResponse:
    try:
        host = _get_host(slug)
        creds = get_credentials(slug)

        if _needs_sudo(host, get_ssh_config()):
            effective_sudo = sudo_password.strip() or creds.get("sudo_password", "")
            if not effective_sudo:
                return templates.TemplateResponse(
                    "partials/sudo_modal.html",
                    {"request": request, "slug": slug, "action": "restart"},
                )
            if sudo_password.strip() and save_sudo == "save":
                save_sudo_password(slug, sudo_password.strip())
            creds = {**creds, "sudo_password": effective_sudo}

        job_id = uuid.uuid4().hex[:8]
        _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
        background_tasks.add_task(_job_run_host_restart, job_id, host, creds)
        return templates.TemplateResponse(
            "partials/job_poll.html",
            {"request": request, "job_id": job_id},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


# ---------------------------------------------------------------------------
# Routes — Docker stacks
# ---------------------------------------------------------------------------

@app.get("/api/docker/check", response_class=HTMLResponse)
async def docker_check(request: Request) -> HTMLResponse:
    hosts = get_hosts()
    backends = get_backends()
    active = [
        b for b in backends
        if b.BACKEND_KEY != "ssh" or any(h.get("docker_mode") for h in hosts)
    ]
    if not active:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "No container backends configured."},
        )
    try:
        results = await asyncio.gather(
            *[b.get_stacks_with_update_status(get_dockerhub_creds()) for b in active],
            return_exceptions=True,
        )
        stacks = []
        for r in results:
            if isinstance(r, list):
                stacks.extend(r)
        # Check for new image updates and fire notifications (deduplicated)
        try:
            from .update_notifier import check_and_notify
            check_and_notify(stacks)
        except Exception:
            pass
        return templates.TemplateResponse(
            "partials/docker_status.html",
            {"request": request, "stacks": stacks},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post("/api/docker/stack/{backend_key}/{ref:path}/update", response_class=HTMLResponse)
async def stack_update(
    request: Request,
    backend_key: str,
    ref: str,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    backend = next((b for b in get_backends() if b.BACKEND_KEY == backend_key), None)
    if not backend:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"Backend {backend_key!r} not configured."},
        )
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    background_tasks.add_task(_job_run_stack_update, job_id, backend_key, ref)
    return templates.TemplateResponse(
        "partials/job_poll.html",
        {"request": request, "job_id": job_id},
    )


# ---------------------------------------------------------------------------
# Routes — Jobs
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str) -> HTMLResponse:
    job = _jobs.get(job_id)
    if not job:
        return HTMLResponse("<span class='text-red-400'>Job not found</span>")
    return templates.TemplateResponse(
        "partials/job_status.html",
        {"request": request, "job_id": job_id, "job": job},
    )


# ---------------------------------------------------------------------------
# Routes — Notifications
# ---------------------------------------------------------------------------

@app.get("/api/notifications/badge", response_class=HTMLResponse)
async def notifications_badge(request: Request) -> HTMLResponse:
    count = get_unread_count()
    if count == 0:
        return HTMLResponse(
            '<span id="notif-badge" hx-get="/api/notifications/badge" '
            'hx-trigger="every 60s" hx-swap="outerHTML"></span>'
        )
    label = str(count) if count < 10 else "9+"
    return HTMLResponse(
        f'<span id="notif-badge" hx-get="/api/notifications/badge" '
        f'hx-trigger="every 60s" hx-swap="outerHTML" '
        f'class="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-red-500 '
        f'text-xs flex items-center justify-center text-white font-bold pointer-events-none">'
        f'{label}</span>'
    )


@app.get("/api/notifications/panel", response_class=HTMLResponse)
async def notifications_panel(request: Request) -> HTMLResponse:
    entries = get_notifications(20)
    return templates.TemplateResponse(
        "partials/notification_panel.html",
        {"request": request, "entries": entries},
    )


@app.post("/api/notifications/read", response_class=HTMLResponse)
async def notifications_read(request: Request) -> HTMLResponse:
    mark_all_read()
    return HTMLResponse(
        '<span id="notif-badge" hx-get="/api/notifications/badge" '
        'hx-trigger="every 60s" hx-swap="outerHTML"></span>'
    )


@app.post("/api/reload-connections", response_class=HTMLResponse)
async def reload_connections(request: Request) -> HTMLResponse:
    """Reinitialise backends after connection settings change."""
    await reload_backends()
    return HTMLResponse("")
