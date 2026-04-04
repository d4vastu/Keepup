import os
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config_manager import (
    add_host,
    delete_host,
    get_hosts,
    get_ssh_config,
    update_host,
    update_ssh_config,
)
from .ssh_client import test_connection

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _connection_status() -> dict:
    return {
        "portainer_url": os.getenv("PORTAINER_URL", ""),
        "portainer_key_set": bool(os.getenv("PORTAINER_API_KEY", "")),
        "dockerhub_user": os.getenv("DOCKERHUB_USERNAME", ""),
        "dockerhub_token_set": bool(os.getenv("DOCKERHUB_TOKEN", "")),
    }


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "hosts": get_hosts(),
            "ssh": get_ssh_config(),
            "conn": _connection_status(),
        },
    )


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@router.get("/hosts", response_class=HTMLResponse)
async def admin_hosts(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": get_hosts()},
    )


@router.post("/hosts", response_class=HTMLResponse)
async def admin_add_host(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    user: str = Form(""),
    port: str = Form(""),
    auth_method: str = Form("key"),
    key: str = Form(""),
    password: str = Form(""),
) -> HTMLResponse:
    try:
        if not name.strip() or not host.strip():
            raise ValueError("Name and IP / hostname are required.")
        add_host(
            name=name.strip(),
            host=host.strip(),
            user=user.strip() or None,
            port=int(port) if port.strip() else None,
            key=key.strip() or None if auth_method == "key" else None,
            password=password.strip() or None if auth_method == "password" else None,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": get_hosts(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": get_hosts()},
    )


@router.get("/hosts/{slug}/edit", response_class=HTMLResponse)
async def admin_edit_host_form(request: Request, slug: str) -> HTMLResponse:
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return HTMLResponse("<span class='text-red-400 text-xs'>Host not found</span>")
    return templates.TemplateResponse(
        "partials/admin_host_edit_form.html",
        {"request": request, "host": host},
    )


@router.put("/hosts/{slug}", response_class=HTMLResponse)
async def admin_update_host(
    request: Request,
    slug: str,
    name: str = Form(...),
    host: str = Form(...),
    user: str = Form(""),
    port: str = Form(""),
    auth_method: str = Form("key"),
    key: str = Form(""),
    password: str = Form(""),
) -> HTMLResponse:
    try:
        update_host(
            slug=slug,
            name=name.strip(),
            host=host.strip(),
            user=user.strip() or None,
            port=int(port) if port.strip() else None,
            key=key.strip() or None if auth_method == "key" else None,
            password=password.strip() or None if auth_method == "password" else None,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": get_hosts(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": get_hosts()},
    )


@router.delete("/hosts/{slug}", response_class=HTMLResponse)
async def admin_delete_host(request: Request, slug: str) -> HTMLResponse:
    try:
        delete_host(slug)
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": get_hosts(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": get_hosts()},
    )


@router.post("/hosts/{slug}/test", response_class=HTMLResponse)
async def admin_test_host(request: Request, slug: str) -> HTMLResponse:
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return HTMLResponse(
            "<span class='text-red-400 text-xs'>Host not found</span>"
        )
    result = await test_connection(host, get_ssh_config())
    return templates.TemplateResponse(
        "partials/admin_host_test_result.html",
        {"request": request, "slug": slug, "result": result},
    )


# ---------------------------------------------------------------------------
# SSH settings
# ---------------------------------------------------------------------------

@router.put("/ssh", response_class=HTMLResponse)
async def admin_update_ssh(
    request: Request,
    default_user: str = Form("root"),
    default_port: str = Form("22"),
    default_key: str = Form("/app/keys/id_ed25519"),
    connect_timeout: str = Form("15"),
    command_timeout: str = Form("600"),
) -> HTMLResponse:
    try:
        update_ssh_config(
            default_user=default_user.strip(),
            default_port=int(default_port),
            default_key=default_key.strip(),
            connect_timeout=int(connect_timeout),
            command_timeout=int(command_timeout),
        )
        saved = True
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_ssh.html",
            {"request": request, "ssh": get_ssh_config(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_ssh.html",
        {"request": request, "ssh": get_ssh_config(), "saved": saved},
    )
