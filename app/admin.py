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
    slugify,
    update_host,
    update_ssh_config,
)
from .credentials import (
    credential_status,
    delete_credentials,
    rename_credentials,
    save_credentials,
)
from .ssh_client import verify_connection

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _connection_status() -> dict:
    return {
        "portainer_url": os.getenv("PORTAINER_URL", ""),
        "portainer_key_set": bool(os.getenv("PORTAINER_API_KEY", "")),
        "dockerhub_user": os.getenv("DOCKERHUB_USERNAME", ""),
        "dockerhub_token_set": bool(os.getenv("DOCKERHUB_TOKEN", "")),
    }


def _hosts_with_status() -> list[dict]:
    return [
        {**h, **credential_status(h["slug"])}
        for h in get_hosts()
    ]


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "hosts": _hosts_with_status(),
            "ssh": get_ssh_config(),
            "conn": _connection_status(),
        },
    )


# ---------------------------------------------------------------------------
# Hosts — CRUD
# ---------------------------------------------------------------------------

@router.get("/hosts", response_class=HTMLResponse)
async def admin_hosts(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status()},
    )


@router.post("/hosts", response_class=HTMLResponse)
async def admin_add_host(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    user: str = Form(""),
    port: str = Form(""),
) -> HTMLResponse:
    try:
        if not name.strip() or not host.strip():
            raise ValueError("Name and IP / hostname are required.")
        slug = add_host(
            name=name.strip(),
            host=host.strip(),
            user=user.strip() or None,
            port=int(port) if port.strip() else None,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": _hosts_with_status(), "error": str(exc)},
        )
    # Return hosts list + open credential form for the new host
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status(), "open_creds": slug},
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
) -> HTMLResponse:
    try:
        new_slug = update_host(
            slug=slug,
            name=name.strip(),
            host=host.strip(),
            user=user.strip() or None,
            port=int(port) if port.strip() else None,
        )
        rename_credentials(slug, new_slug)
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": _hosts_with_status(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status()},
    )


@router.delete("/hosts/{slug}", response_class=HTMLResponse)
async def admin_delete_host(request: Request, slug: str) -> HTMLResponse:
    try:
        delete_host(slug)
        delete_credentials(slug)
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/admin_hosts.html",
            {"request": request, "hosts": _hosts_with_status(), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status()},
    )


# ---------------------------------------------------------------------------
# Hosts — credentials
# ---------------------------------------------------------------------------

@router.get("/hosts/{slug}/credentials", response_class=HTMLResponse)
async def admin_credentials_form(request: Request, slug: str) -> HTMLResponse:
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return HTMLResponse("<span class='text-red-400 text-xs'>Host not found</span>")
    status = credential_status(slug)
    return templates.TemplateResponse(
        "partials/admin_host_credentials.html",
        {"request": request, "host": host, "status": status},
    )


@router.post("/hosts/{slug}/credentials", response_class=HTMLResponse)
async def admin_save_credentials(
    request: Request,
    slug: str,
    auth_method: str = Form("password"),
    ssh_password: str = Form(""),
    ssh_key: str = Form(""),
    sudo_password: str = Form(""),
) -> HTMLResponse:
    try:
        save_credentials(
            slug=slug,
            ssh_password=ssh_password.strip() or None if auth_method == "password" else "",
            ssh_key=ssh_key.strip() or None if auth_method == "key" else "",
            sudo_password=sudo_password.strip() or None,
        )
    except Exception as exc:
        hosts = get_hosts()
        host = next((h for h in hosts if h["slug"] == slug), {})
        return templates.TemplateResponse(
            "partials/admin_host_credentials.html",
            {"request": request, "host": host, "status": credential_status(slug), "error": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status()},
    )


# ---------------------------------------------------------------------------
# Hosts — connection test
# ---------------------------------------------------------------------------

@router.post("/hosts/{slug}/test", response_class=HTMLResponse)
async def admin_test_host(request: Request, slug: str) -> HTMLResponse:
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return HTMLResponse("<span class='text-red-400 text-xs'>Host not found</span>")
    from .credentials import get_credentials
    result = await verify_connection(host, get_ssh_config(), get_credentials(slug))
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
