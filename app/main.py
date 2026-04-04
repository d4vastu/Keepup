import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .admin import router as admin_router
from .config_manager import get_hosts, get_ssh_config
from .portainer_client import PortainerClient
from .ssh_client import check_host_updates, reboot_host, run_host_update_buffered

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Update Dashboard")
app.include_router(admin_router)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------------------
# Config — secrets from env vars, structure from config.yml
# ---------------------------------------------------------------------------

portainer: PortainerClient | None = None

# DockerHub credentials (optional, raises registry pull rate limit)
_dockerhub_user = os.getenv("DOCKERHUB_USERNAME", "")
_dockerhub_token = os.getenv("DOCKERHUB_TOKEN", "")
dockerhub_creds: dict | None = (
    {"username": _dockerhub_user, "token": _dockerhub_token}
    if _dockerhub_user and _dockerhub_token
    else None
)


@app.on_event("startup")
async def _startup() -> None:
    global portainer
    url = os.getenv("PORTAINER_URL", "")
    key = os.getenv("PORTAINER_API_KEY", "")
    verify_ssl = os.getenv("PORTAINER_VERIFY_SSL", "false").lower() == "true"
    if url and key:
        portainer = PortainerClient(url=url, api_key=key, verify_ssl=verify_ssl)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_host(slug: str) -> dict:
    for h in get_hosts():
        if h["slug"] == slug:
            return h
    raise KeyError(f"Host {slug!r} not in config")


# In-memory job store  {job_id: {"done": bool, "error": str|None, "lines": [str]}}
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------

async def _job_run_host_update(job_id: str, host: dict) -> None:
    try:
        lines = await run_host_update_buffered(host, get_ssh_config())
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_host_restart(job_id: str, host: dict) -> None:
    try:
        lines = await reboot_host(host, get_ssh_config())
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_stack_update(job_id: str, stack_id: int, endpoint_id: int) -> None:
    try:
        await portainer.update_stack(stack_id, endpoint_id)
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
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "hosts": get_hosts(), "portainer_configured": portainer is not None},
    )


# ---------------------------------------------------------------------------
# Routes — OS updates
# ---------------------------------------------------------------------------

@app.get("/api/host/{slug}/check", response_class=HTMLResponse)
async def host_check(request: Request, slug: str) -> HTMLResponse:
    try:
        host = _get_host(slug)
        result = await check_host_updates(host, get_ssh_config())
        return templates.TemplateResponse(
            "partials/host_status.html",
            {
                "request": request,
                "slug": slug,
                "packages": result["packages"],
                "reboot_required": result["reboot_required"],
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post("/api/host/{slug}/update", response_class=HTMLResponse)
async def host_update(
    request: Request, slug: str, background_tasks: BackgroundTasks
) -> HTMLResponse:
    try:
        host = _get_host(slug)
        job_id = uuid.uuid4().hex[:8]
        _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
        background_tasks.add_task(_job_run_host_update, job_id, host)
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
    request: Request, slug: str, background_tasks: BackgroundTasks
) -> HTMLResponse:
    try:
        host = _get_host(slug)
        job_id = uuid.uuid4().hex[:8]
        _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
        background_tasks.add_task(_job_run_host_restart, job_id, host)
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
    if not portainer:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Portainer API key not configured."},
        )
    try:
        stacks = await portainer.get_stacks_with_update_status(dockerhub_creds)
        return templates.TemplateResponse(
            "partials/docker_status.html",
            {"request": request, "stacks": stacks},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post("/api/docker/stack/{stack_id}/update", response_class=HTMLResponse)
async def stack_update(
    request: Request,
    stack_id: int,
    endpoint_id: int,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    if not portainer:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Portainer not configured."},
        )
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"done": False, "status": "running", "error": None, "lines": []}
    background_tasks.add_task(_job_run_stack_update, job_id, stack_id, endpoint_id)
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
