import asyncio
import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .admin import router as admin_router
from .config_manager import get_hosts, get_ssh_config
from .credentials import get_credentials, save_sudo_password
from .ssh_client import _needs_sudo, check_host_updates, reboot_host, run_host_update_buffered

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Update Dashboard")
app.include_router(admin_router)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_backends: list = []

_dockerhub_user = os.getenv("DOCKERHUB_USERNAME", "")
_dockerhub_token = os.getenv("DOCKERHUB_TOKEN", "")
dockerhub_creds: dict | None = (
    {"username": _dockerhub_user, "token": _dockerhub_token}
    if _dockerhub_user and _dockerhub_token
    else None
)


@app.on_event("startup")
async def _startup() -> None:
    global _backends
    backends = []

    # Portainer backend — opt-in via env vars
    url = os.getenv("PORTAINER_URL", "")
    key = os.getenv("PORTAINER_API_KEY", "")
    verify_ssl = os.getenv("PORTAINER_VERIFY_SSL", "false").lower() == "true"
    if url and key:
        from .portainer_client import PortainerClient
        from .backends import PortainerBackend
        backends.append(PortainerBackend(PortainerClient(url=url, api_key=key, verify_ssl=verify_ssl)))

    # SSH Docker backend — always registered; only activates for hosts with docker_mode set
    from .backends import SSHDockerBackend
    backends.append(SSHDockerBackend())

    _backends = backends


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
        backend = next((b for b in _backends if b.BACKEND_KEY == backend_key), None)
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
async def dashboard(request: Request) -> HTMLResponse:
    hosts = get_hosts()
    docker_configured = (
        any(b.BACKEND_KEY == "portainer" for b in _backends)
        or any(h.get("docker_mode") for h in hosts)
    )
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "hosts": hosts, "docker_configured": docker_configured},
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
    active = [
        b for b in _backends
        if b.BACKEND_KEY != "ssh" or any(h.get("docker_mode") for h in hosts)
    ]
    if not active:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "No container backends configured."},
        )
    try:
        results = await asyncio.gather(
            *[b.get_stacks_with_update_status(dockerhub_creds) for b in active],
            return_exceptions=True,
        )
        stacks = []
        for r in results:
            if isinstance(r, list):
                stacks.extend(r)
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
    backend = next((b for b in _backends if b.BACKEND_KEY == backend_key), None)
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
