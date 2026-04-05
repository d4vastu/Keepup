import asyncio
import os
from datetime import datetime, timezone

import httpx
import pyotp
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

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
    clear_ssl_config,
    delete_host,
    get_dockerhub_config,
    get_homeassistant_config,
    get_hosts,
    get_opnsense_config,
    get_pbs_config,
    get_pfsense_config,
    get_portainer_config,
    get_proxmox_config,
    get_pushover_config,
    get_ssl_config,
    get_ssh_config,
    get_timezone,
    reset_config,
    save_dockerhub_config,
    save_portainer_config,
    save_pushover_config,
    save_ssl_config,
    save_timezone,
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
from .auto_update_log import get_recent
from .log_buffer import get_log_lines
from .templates_env import make_templates

router = APIRouter(prefix="/admin")
templates = make_templates()


def _connection_status() -> dict:
    """Read connection status from the UI-managed credential store."""
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    dh_cfg = get_dockerhub_config()
    dh_creds = get_integration_credentials("dockerhub")
    pushover_cfg = get_pushover_config()
    pushover_creds = get_integration_credentials("pushover")

    return {
        "portainer_url": port_cfg.get("url", ""),
        "portainer_key_set": bool(port_creds.get("api_key")),
        "portainer_verify_ssl": port_cfg.get("verify_ssl", False),
        "portainer_env_only": False,
        "dockerhub_user": dh_cfg.get("username", ""),
        "dockerhub_token_set": bool(dh_creds.get("token")),
        "pushover_token_set": bool(pushover_creds.get("api_token")),
        "pushover_user_set": bool(pushover_creds.get("user_key")),
        "pushover_enabled": pushover_cfg.get("enabled", False),
    }


def _integration_status() -> dict:
    """Read status for all API integrations."""
    px_cfg = get_proxmox_config()
    px_creds = get_integration_credentials("proxmox")
    pbs_cfg = get_pbs_config()
    pbs_creds = get_integration_credentials("proxmox_backup")
    opn_cfg = get_opnsense_config()
    opn_creds = get_integration_credentials("opnsense")
    pf_cfg = get_pfsense_config()
    pf_creds = get_integration_credentials("pfsense")
    ha_cfg = get_homeassistant_config()
    ha_creds = get_integration_credentials("homeassistant")
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    dh_cfg = get_dockerhub_config()
    dh_creds = get_integration_credentials("dockerhub")

    return {
        "proxmox_url": px_cfg.get("url", ""),
        "proxmox_api_user": px_creds.get("api_user", ""),
        "proxmox_token_id": px_creds.get("token_id", ""),
        "proxmox_secret_set": bool(px_creds.get("secret") or px_creds.get("api_token")),
        "proxmox_configured": bool(px_cfg.get("url") and (px_creds.get("secret") or px_creds.get("api_token"))),
        "proxmox_verify_ssl": px_cfg.get("verify_ssl", False),
        "pbs_url": pbs_cfg.get("url", ""),
        "pbs_api_user": pbs_creds.get("api_user", ""),
        "pbs_token_id": pbs_creds.get("token_id", ""),
        "pbs_secret_set": bool(pbs_creds.get("secret") or pbs_creds.get("api_token")),
        "pbs_configured": bool(pbs_cfg.get("url") and (pbs_creds.get("secret") or pbs_creds.get("api_token"))),
        "pbs_verify_ssl": pbs_cfg.get("verify_ssl", False),
        "opnsense_url": opn_cfg.get("url", ""),
        "opnsense_configured": bool(opn_cfg.get("url") and opn_creds.get("api_key")),
        "opnsense_verify_ssl": opn_cfg.get("verify_ssl", False),
        "pfsense_url": pf_cfg.get("url", ""),
        "pfsense_configured": bool(pf_cfg.get("url") and pf_creds.get("api_key")),
        "pfsense_verify_ssl": pf_cfg.get("verify_ssl", False),
        "ha_url": ha_cfg.get("url", ""),
        "ha_configured": bool(ha_cfg.get("url") and ha_creds.get("token")),
        "portainer_url": port_cfg.get("url", ""),
        "portainer_key_set": bool(port_creds.get("api_key")),
        "portainer_verify_ssl": port_cfg.get("verify_ssl", False),
        "portainer_configured": bool(port_cfg.get("url") and port_creds.get("api_key")),
        "dockerhub_user": dh_cfg.get("username", ""),
        "dockerhub_token_set": bool(dh_creds.get("token")),
        "dockerhub_configured": bool(dh_cfg.get("username") and dh_creds.get("token")),
    }


def _hosts_with_status() -> list[dict]:
    return [{**h, **credential_status(h["slug"])} for h in get_hosts()]


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    from fastapi.responses import RedirectResponse as _Redirect

    return _Redirect("/admin/connections", status_code=302)


@router.get("/integrations", response_class=HTMLResponse)
async def admin_integrations(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin_integrations.html",
        {"request": request, "integ": _integration_status(), "app_version": APP_VERSION},
    )


@router.post("/integrations/portainer/test", response_class=HTMLResponse)
async def admin_integrations_test_portainer(
    request: Request,
    portainer_url: str = Form(""),
    portainer_api_key: str = Form(""),
    portainer_verify_ssl: str = Form(""),
) -> HTMLResponse:
    """Test Portainer connection — same logic as connections test, returns inline HTML."""
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
        elif "Name or service not known" in msg or "connect" in msg.lower():
            hint = "Can't reach that address — check the URL and that Portainer is running."
        elif "SSL" in msg or "certificate" in msg.lower():
            hint = "SSL error — try disabling SSL verification."
        else:
            hint = msg
        return HTMLResponse(f'<span class="text-red-400 text-sm">&#10007; {hint}</span>')


@router.post("/integrations/portainer", response_class=HTMLResponse)
async def admin_integrations_save_portainer(
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
        "partials/admin_integrations.html",
        {"request": request, "integ": _integration_status(), "portainer_saved": True},
    )


@router.post("/integrations/dockerhub", response_class=HTMLResponse)
async def admin_integrations_save_dockerhub(
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
        "partials/admin_integrations.html",
        {"request": request, "integ": _integration_status(), "dockerhub_saved": True},
    )


@router.get("/connections", response_class=HTMLResponse)
async def admin_connections(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "conn": _connection_status(), "app_version": APP_VERSION},
    )


@router.get("/hosts", response_class=HTMLResponse)
async def admin_hosts_page(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin_hosts.html",
        {"request": request, "hosts": _hosts_with_status(), "app_version": APP_VERSION},
    )


@router.get("/ssh", response_class=HTMLResponse)
async def admin_ssh_page(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin_ssh.html",
        {"request": request, "ssh": get_ssh_config(), "app_version": APP_VERSION},
    )


# ---------------------------------------------------------------------------
# Hosts — CRUD
# ---------------------------------------------------------------------------


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
            ssh_password=ssh_password.strip() or None
            if auth_method == "password"
            else "",
            ssh_key=ssh_key.strip() or None if auth_method == "key" else "",
            sudo_password=sudo_password.strip() or None,
        )
    except Exception as exc:
        hosts = get_hosts()
        host = next((h for h in hosts if h["slug"] == slug), {})
        return templates.TemplateResponse(
            "partials/admin_host_credentials.html",
            {
                "request": request,
                "host": host,
                "status": credential_status(slug),
                "error": str(exc),
            },
        )
    # Return hosts list and trigger Docker auto-discovery for this host
    return templates.TemplateResponse(
        "partials/admin_hosts.html",
        {
            "request": request,
            "hosts": _hosts_with_status(),
            "discover_docker": slug,  # triggers auto-discovery in the template
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
        elif (
            "Name or service not known" in msg
            or "ConnectionRefused" in msg.lower()
            or "connect" in msg.lower()
        ):
            hint = "Can't reach that address — check the URL and that Portainer is running."
        elif "SSL" in msg or "certificate" in msg.lower():
            hint = "SSL error — try enabling &ldquo;Ignore SSL warnings&rdquo; below."
        else:
            hint = msg
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


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


# ---------------------------------------------------------------------------
# Connections — Pushover
# ---------------------------------------------------------------------------


@router.post("/connections/pushover/test", response_class=HTMLResponse)
async def admin_test_pushover(request: Request) -> HTMLResponse:
    from .pushover import send_pushover

    success = await send_pushover("Test", "Keepup test notification")
    if success:
        return HTMLResponse(
            '<span class="text-green-400 text-sm">&#10003; Test notification sent successfully.</span>'
        )
    return HTMLResponse(
        '<span class="text-red-400 text-sm">&#10007; Failed — check your API token and user key.</span>'
    )


@router.post("/connections/pushover", response_class=HTMLResponse)
async def admin_save_pushover(
    request: Request,
    pushover_api_token: str = Form(""),
    pushover_user_key: str = Form(""),
    pushover_enabled: str = Form(""),
) -> HTMLResponse:
    token = pushover_api_token.strip()
    user_key = pushover_user_key.strip()
    enabled = pushover_enabled == "on"

    save_pushover_config(enabled=enabled)
    if token:
        save_integration_credentials("pushover", api_token=token)
    if user_key:
        save_integration_credentials("pushover", user_key=user_key)

    return templates.TemplateResponse(
        "partials/admin_connections.html",
        {"request": request, "conn": _connection_status(), "pushover_saved": True},
    )


# ---------------------------------------------------------------------------
# About page
# ---------------------------------------------------------------------------


@router.get("/about", response_class=HTMLResponse)
async def admin_about(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    releases = []
    latest_version = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                "https://api.github.com/repos/d4vastu/Keepup/releases",
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "keepup",
                },
            )
            if resp.status_code == 200:
                raw = resp.json()[:10]
                now = datetime.now(timezone.utc)
                for r in raw:
                    pub = datetime.fromisoformat(
                        r["published_at"].replace("Z", "+00:00")
                    )
                    delta = now - pub
                    days = delta.days
                    if days == 0:
                        age = "today"
                    elif days == 1:
                        age = "yesterday"
                    elif days < 30:
                        age = f"{days} days ago"
                    elif days < 365:
                        age = f"{days // 30} months ago"
                    else:
                        age = f"{days // 365} years ago"
                    releases.append({**r, "age": age})
                if releases:
                    latest_version = releases[0]["tag_name"]
    except Exception:
        pass

    data_dir = str(os.getenv("DATA_PATH", "/app/data"))
    return templates.TemplateResponse(
        "admin_about.html",
        {
            "request": request,
            "app_version": APP_VERSION,
            "host_count": len(get_hosts()),
            "data_dir": data_dir,
            "timezone": get_timezone(),
            "releases": releases,
            "latest_version": latest_version,
            "is_latest": latest_version == f"v{APP_VERSION}"
            if latest_version
            else False,
        },
    )


# ---------------------------------------------------------------------------
# Auto-update history
# ---------------------------------------------------------------------------


@router.get("/auto-updates/history", response_class=HTMLResponse)
async def admin_auto_update_history(request: Request) -> HTMLResponse:
    entries = get_recent(100)
    return templates.TemplateResponse(
        "admin_auto_update_history.html",
        {"request": request, "entries": entries},
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

_COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Anchorage",
    "America/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "America/Sao_Paulo",
    "America/Argentina/Buenos_Aires",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Rome",
    "Europe/Madrid",
    "Europe/Amsterdam",
    "Europe/Stockholm",
    "Europe/Warsaw",
    "Europe/Helsinki",
    "Europe/Athens",
    "Europe/Istanbul",
    "Europe/Moscow",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Dhaka",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Pacific/Auckland",
]


def _account_context() -> dict:
    return {
        "mfa_enrolled": mfa_enrolled(),
        "admin_username": get_admin_username(),
        "timezone": get_timezone(),
        "common_timezones": _COMMON_TIMEZONES,
    }


@router.get("/account", response_class=HTMLResponse)
async def admin_account(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin_account.html",
        {"request": request, **_account_context(), "app_version": APP_VERSION},
    )


@router.post("/account/timezone", response_class=HTMLResponse)
async def admin_save_timezone(
    request: Request,
    timezone: str = Form("UTC"),
) -> HTMLResponse:
    tz = timezone.strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)  # validate
        save_timezone(tz)
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {"request": request, **_account_context(), "tz_saved": True},
        )
    except Exception:
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {
                "request": request,
                **_account_context(),
                "tz_error": f"Unknown timezone: {tz!r}",
            },
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
            {
                "request": request,
                **_account_context(),
                "mfa_remove_error": "Password or code incorrect.",
            },
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
            {
                "request": request,
                **_account_context(),
                "bk_error": "Current password is incorrect.",
            },
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
            {
                "request": request,
                **_account_context(),
                "reset_error": "Password is incorrect.",
            },
        )
    if confirm_text.strip().upper() != "RESET":
        return templates.TemplateResponse(
            "partials/admin_account.html",
            {
                "request": request,
                **_account_context(),
                "reset_error": 'Type "RESET" in the confirmation field.',
            },
        )
    wipe_credential_store()
    reset_config()
    request.session.clear()
    from fastapi.responses import Response

    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/setup"
    return resp


# ---------------------------------------------------------------------------
# HTTPS / TLS
# ---------------------------------------------------------------------------


async def _restart_after_delay() -> None:
    """Send SIGTERM to PID 1 (uvicorn) after a brief delay so the response is sent first."""
    import signal

    await asyncio.sleep(3)
    os.kill(1, signal.SIGTERM)


def _ssl_context() -> dict:
    from .ssl_manager import ssl_enabled, get_cert_info

    ssl_cfg = get_ssl_config()
    return {
        "ssl_enabled": ssl_enabled(),
        "ssl_mode": ssl_cfg.get("mode", ""),
        "ssl_hostname": ssl_cfg.get("hostname", ""),
        "cert_info": get_cert_info(),
    }


@router.get("/https", response_class=HTMLResponse)
async def admin_https(request: Request) -> HTMLResponse:
    from .__version__ import APP_VERSION

    return templates.TemplateResponse(
        "admin_https.html",
        {"request": request, **_ssl_context(), "app_version": APP_VERSION},
    )


@router.post("/https/self-signed", response_class=HTMLResponse)
async def admin_https_self_signed(
    request: Request,
    hostname: str = Form(""),
) -> HTMLResponse:
    from .ssl_manager import generate_self_signed_cert, save_ssl_files

    hostname = hostname.strip()
    if not hostname:
        return templates.TemplateResponse(
            "partials/admin_https.html",
            {
                "request": request,
                **_ssl_context(),
                "error": "Enter your server's IP address or hostname.",
            },
        )
    cert_pem, key_pem = generate_self_signed_cert(hostname)
    save_ssl_files(cert_pem, key_pem)
    save_ssl_config(mode="self-signed", hostname=hostname)
    asyncio.ensure_future(_restart_after_delay())
    return templates.TemplateResponse(
        "partials/admin_https_restarting.html",
        {
            "request": request,
            "new_url": f"https://{hostname}:8765",
            "action": "enabling",
        },
    )


@router.post("/https/disable", response_class=HTMLResponse)
async def admin_https_disable(request: Request) -> HTMLResponse:
    from .ssl_manager import remove_ssl_files

    remove_ssl_files()
    clear_ssl_config()
    asyncio.ensure_future(_restart_after_delay())
    host = request.headers.get("host", "").split(":")[0]
    return templates.TemplateResponse(
        "partials/admin_https_restarting.html",
        {"request": request, "new_url": f"http://{host}:8765", "action": "disabling"},
    )


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin_logs.html",
        {"request": request, "lines": get_log_lines()},
    )


@router.get("/logs/lines", response_class=HTMLResponse)
async def admin_logs_lines(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/admin_log_lines.html",
        {"request": request, "lines": get_log_lines()},
    )
