import asyncio
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .auto_update_scheduler import apply_host_schedule, apply_stack_schedule
from .config_manager import (
    get_all_stack_auto_updates,
    get_hosts,
    set_host_auto_update,
    set_stack_auto_update,
)

router = APIRouter(prefix="/admin/auto-updates")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Injected by main.py so stack discovery can reach the backends
_backends: list = []


def set_backends(backends: list) -> None:
    global _backends
    _backends = backends


def _validate_cron(expr: str) -> str | None:
    """Return error string if expr is not a valid 5-field cron, else None."""
    try:
        CronTrigger.from_crontab(expr)
        return None
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def auto_updates_page(request: Request) -> HTMLResponse:
    hosts = get_hosts()
    return templates.TemplateResponse(
        "auto_updates.html",
        {"request": request, "hosts": hosts},
    )


# ---------------------------------------------------------------------------
# OS / host settings
# ---------------------------------------------------------------------------

@router.post("/hosts/{slug}", response_class=HTMLResponse)
async def save_host_auto_update(
    request: Request,
    slug: str,
    os_enabled: str = Form(""),
    os_schedule: str = Form("0 3 * * *"),
    auto_reboot: str = Form(""),
) -> HTMLResponse:
    enabled = os_enabled == "on"
    reboot = auto_reboot == "on"
    schedule = os_schedule.strip() or "0 3 * * *"

    if enabled:
        err = _validate_cron(schedule)
        if err:
            hosts = get_hosts()
            host = next((h for h in hosts if h["slug"] == slug), {})
            return templates.TemplateResponse(
                "partials/auto_update_host_row.html",
                {"request": request, "host": host, "error": f"Invalid schedule: {err}"},
            )

    set_host_auto_update(slug, enabled, schedule, reboot)
    apply_host_schedule(slug)

    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), {})
    return templates.TemplateResponse(
        "partials/auto_update_host_row.html",
        {"request": request, "host": host, "saved": True},
    )


# ---------------------------------------------------------------------------
# Docker stack settings
# ---------------------------------------------------------------------------

@router.get("/stacks", response_class=HTMLResponse)
async def auto_update_stacks(request: Request) -> HTMLResponse:
    """Load all known stacks from backends and overlay auto-update config."""
    if not _backends:
        return templates.TemplateResponse(
            "partials/auto_update_stacks.html",
            {"request": request, "stacks": [], "no_backends": True},
        )
    try:
        results = await asyncio.gather(
            *[b.get_stacks_with_update_status() for b in _backends],
            return_exceptions=True,
        )
        stacks = []
        for r in results:
            if isinstance(r, list):
                stacks.extend(r)
        sau = get_all_stack_auto_updates()
        for s in stacks:
            cfg = sau.get(s["update_path"], {})
            s["auto_enabled"] = cfg.get("enabled", False)
            s["auto_schedule"] = cfg.get("schedule", "0 4 * * *")
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/auto_update_stacks.html",
        {"request": request, "stacks": stacks},
    )


@router.post("/stacks/{backend_key}/{ref:path}", response_class=HTMLResponse)
async def save_stack_auto_update(
    request: Request,
    backend_key: str,
    ref: str,
    stack_name: str = Form(""),
    enabled: str = Form(""),
    schedule: str = Form("0 4 * * *"),
) -> HTMLResponse:
    is_enabled = enabled == "on"
    clean_schedule = schedule.strip() or "0 4 * * *"
    update_path = f"{backend_key}/{ref}"

    if is_enabled:
        err = _validate_cron(clean_schedule)
        if err:
            return HTMLResponse(
                f'<span class="text-red-400 text-xs">Invalid schedule: {err}</span>'
            )

    set_stack_auto_update(update_path, stack_name, is_enabled, clean_schedule)
    apply_stack_schedule(update_path)

    sau = get_all_stack_auto_updates()
    cfg = sau.get(update_path, {})
    return templates.TemplateResponse(
        "partials/auto_update_stack_row.html",
        {
            "request": request,
            "stack": {
                "update_path": update_path,
                "name": stack_name,
                "backend_key": backend_key,
                "ref": ref,
                "auto_enabled": cfg.get("enabled", False),
                "auto_schedule": cfg.get("schedule", clean_schedule),
            },
            "saved": True,
        },
    )
