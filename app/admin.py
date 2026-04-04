import os
from pathlib import Path

import pyotp
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    change_password,
    enroll_mfa,
    get_admin_username,
    get_totp_uri,
    mfa_enrolled,
    new_totp_secret,
    regenerate_backup_key,
    remove_mfa,
    verify_password,
    verify_totp,
)
from .backend_loader import reload_backends
from .config_manager import (
    add_host,
    delete_host,
    get_dockerhub_config,
    get_hosts,
    get_portainer_config,
    get_ssh_config,
    reset_config,
    save_dockerhub_config,
    save_portainer_config,
    set_docker_monitoring,
    update_host,
    update_ssh_config,
)
from .credentials import (
    credential_status,
    delete_credentials,
    get_integration_credentials,
    rename_credentials,
    save_credentials,
    save_integration_credentials,
    wipe_credential_store,
)
from .ssh_client import verify_connection

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _connection_status() -> dict:
    """Read connection status from credential store, fall back to env vars."""
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    dh_cfg = get_dockerhub_config()
    dh_creds = get_integration_credentials("dockerhub")

    portainer_url = port_cfg.get("url") or os.getenv("PORTAINER_URL", "")
    portainer_key_set = bool(port_creds.get("api_key") or os.getenv("PORTAINER_API_KEY", ""))
    portainer_verify_ssl = port_cfg.get("verify_ssl", False)
    dockerhub_user = dh_cfg.get("username") or os.getenv("DOCKERHUB_USERNAME", "")
    dockerhub_token_set = bool(dh_creds.get("token") or os.getenv("DOCKERHUB_TOKEN", ""))

    # If values only exist in env vars, flag for migration nudge
    portainer_env_only = (
        not port_cfg.get("url") and bool(os.getenv("PORTAINER_URL", ""))
    )

    return {
        "portainer_url": portainer_url,
        "portainer_key_set": portainer_key_set,
        "portainer_verify_ssl": portainer_verify_ssl,
        "portainer_env_only": portainer_env_only,
        "dockerhub_user": dockerhub_user,
        "dockerhub_token_set": dockerhub_token_set,
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
            **_account_context(),
        },
    )


@router.get("/connections", response_class=HTMLResponse)
async def admin_connections(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/admin_connections.html",
        {"request": request, "conn": _connection_status()},
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
    # Return hosts list and trigger Docker auto-discovery for this host
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {
            "request": request,
            "hosts": _hosts_with_status(),
            "discover_docker": slug,   # triggers auto-discovery in the template
        },
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

# ---------------------------------------------------------------------------
# Docker monitoring — discover and configure
# ---------------------------------------------------------------------------

@router.delete("/hosts/{slug}/docker-prompt", response_class=HTMLResponse)
async def admin_dismiss_docker_prompt(request: Request, slug: str) -> HTMLResponse:
    """Dismiss the Docker discovery prompt without saving anything."""
    return HTMLResponse("")


@router.get("/hosts/{slug}/docker-discover", response_class=HTMLResponse)
async def admin_docker_discover(request: Request, slug: str) -> HTMLResponse:
    """SSH into the host and return found Compose stacks, or empty if none."""
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return HTMLResponse("")
    from .backends.ssh_docker_backend import SSHDockerBackend
    backend = SSHDockerBackend()
    stacks = await backend.discover_stacks(host)
    if not stacks:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "partials/admin_docker_prompt.html",
        {"request": request, "slug": slug, "host": host, "stacks": stacks},
    )


@router.post("/hosts/{slug}/docker-monitoring", response_class=HTMLResponse)
async def admin_save_docker_monitoring(
    request: Request,
    slug: str,
    docker_mode: str = Form("none"),
    docker_stacks: list[str] = Form(default=[]),
) -> HTMLResponse:
    try:
        set_docker_monitoring(
            slug=slug,
            mode=docker_mode,
            stacks=docker_stacks if docker_mode == "selected" else None,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status()},
    )


# ---------------------------------------------------------------------------
# Connections — Portainer
# ---------------------------------------------------------------------------

@router.post("/connections/portainer/test", response_class=HTMLResponse)
async def admin_test_portainer(
    request: Request,
    portainer_url: str = Form(""),
    portainer_api_key: str = Form(""),
    portainer_verify_ssl: str = Form(""),
) -> HTMLResponse:
    """Test Portainer connection with provided (unsaved) values."""
    url = portainer_url.strip().rstrip("/")
    key = portainer_api_key.strip()
    verify_ssl = portainer_verify_ssl == "on"

    if not url or not key:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter a URL and API token first.</span>'
        )
    try:
        from .portainer_client import PortainerClient
        client = PortainerClient(url=url, api_key=key, verify_ssl=verify_ssl)
        endpoints = await client.get_endpoints()
        count = len(endpoints)
        return HTMLResponse(
            f'<span class="text-green-400 text-sm">&#10003; Connected — '
            f'{count} environment{"s" if count != 1 else ""} found. '
            f'Click Save to apply.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "403" in msg:
            hint = "Invalid API token — check you copied it correctly."
        elif "Name or service not known" in msg or "ConnectionRefused" in msg.lower() or "connect" in msg.lower():
            hint = "Can't reach that address — check the URL and that Portainer is running."
        elif "SSL" in msg or "certificate" in msg.lower():
            hint = "SSL error — try enabling &ldquo;Ignore SSL warnings&rdquo; below."
        else:
            hint = msg
        return HTMLResponse(f'<span class="text-red-400 text-sm">&#10007; {hint}</span>')


@router.post("/connections/portainer", response_class=HTMLResponse)
async def admin_save_portainer(
    request: Request,
    portainer_url: str = Form(""),
    portainer_api_key: str = Form(""),
    portainer_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = portainer_url.strip().rstrip("/")
    key = portainer_api_key.strip()
    verify_ssl = portainer_verify_ssl == "on"

    save_portainer_config(url=url, verify_ssl=verify_ssl)
    if key:
        save_integration_credentials("portainer", api_key=key)

    await reload_backends()

    return templates.TemplateResponse(
        "partials/admin_connections.html",
        {"request": request, "conn": _connection_status(), "portainer_saved": True},
    )


# ---------------------------------------------------------------------------
# Connections — DockerHub
# ---------------------------------------------------------------------------

@router.post("/connections/dockerhub", response_class=HTMLResponse)
async def admin_save_dockerhub(
    request: Request,
    dockerhub_username: str = Form(""),
    dockerhub_token: str = Form(""),
) -> HTMLResponse:
    username = dockerhub_username.strip()
    token = dockerhub_token.strip()

    save_dockerhub_config(username=username)
    if token:
        save_integration_credentials("dockerhub", token=token)
    elif not username:
        save_integration_credentials("dockerhub", token="")

    await reload_backends()

    return templates.TemplateResponse(
        "partials/admin_connections.html",
        {"request": request, "conn": _connection_status(), "dockerhub_saved": True},
    )


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


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

def _account_context() -> dict:
    return {"mfa_enrolled": mfa_enrolled(), "admin_username": get_admin_username()}


@router.get("/account", response_class=HTMLResponse)
async def admin_account(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {"request": request, **_account_context()},
    )


@router.post("/account/password", response_class=HTMLResponse)
async def admin_change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
) -> HTMLResponse:
    errors: list[str] = []
    if not verify_password(current_password):
        errors.append("Current password is incorrect.")
    if len(new_password) < 8:
        errors.append("New password must be at least 8 characters.")
    if new_password != new_password_confirm:
        errors.append("New passwords do not match.")
    if errors:
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "pw_errors": errors},
        )
    change_password(new_password)
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {"request": request, **_account_context(), "pw_saved": True},
    )


@router.get("/account/mfa/setup", response_class=HTMLResponse)
async def admin_mfa_setup_page(request: Request) -> HTMLResponse:
    secret = new_totp_secret()
    request.session["mfa_setup_secret"] = secret
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {
            "request": request,
            **_account_context(),
            "mfa_setup": True,
            "totp_uri": get_totp_uri(secret),
            "totp_secret": secret,
        },
    )


@router.post("/account/mfa/setup", response_class=HTMLResponse)
async def admin_mfa_setup_submit(
    request: Request,
    totp_code: str = Form(""),
) -> HTMLResponse:
    secret = request.session.get("mfa_setup_secret", "")
    if not secret or not pyotp.TOTP(secret).verify(totp_code.strip(), valid_window=1):
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {
                "request": request,
                **_account_context(),
                "mfa_setup": True,
                "totp_uri": get_totp_uri(secret) if secret else "",
                "totp_secret": secret,
                "mfa_error": "Code is incorrect — make sure your phone's time is synced and try again.",
            },
        )
    enroll_mfa(secret)
    request.session.pop("mfa_setup_secret", None)
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {"request": request, **_account_context(), "mfa_enrolled_now": True},
    )


@router.post("/account/mfa/remove", response_class=HTMLResponse)
async def admin_mfa_remove(
    request: Request,
    current_password: str = Form(""),
    totp_code: str = Form(""),
) -> HTMLResponse:
    if not verify_password(current_password) or not verify_totp(totp_code):
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "mfa_remove_error": "Password or code incorrect."},
        )
    remove_mfa()
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {"request": request, **_account_context(), "mfa_removed": True},
    )


@router.post("/account/backup-key", response_class=HTMLResponse)
async def admin_regenerate_backup_key(
    request: Request,
    current_password: str = Form(""),
) -> HTMLResponse:
    if not verify_password(current_password):
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "bk_error": "Current password is incorrect."},
        )
    new_key = regenerate_backup_key()
    return templates.TemplateResponse(
        "partials/admin_account.html",
        {"request": request, **_account_context(), "new_backup_key": new_key},
    )


@router.post("/account/factory-reset", response_class=HTMLResponse)
async def admin_factory_reset(
    request: Request,
    current_password: str = Form(""),
    confirm_text: str = Form(""),
) -> HTMLResponse:
    if not verify_password(current_password):
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "reset_error": "Password is incorrect."},
        )
    if confirm_text.strip().upper() != "RESET":
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "reset_error": 'Type "RESET" in the confirmation field.'},
        )
    wipe_credential_store()
    reset_config()
    request.session.clear()
    from fastapi.responses import Response
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/setup"
    return resp
