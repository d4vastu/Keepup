import re
import time
from collections import defaultdict
from zoneinfo import available_timezones

import httpx
import pyotp
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import (
    admin_exists,
    create_admin,
    enroll_mfa,
    get_totp_uri,
    mfa_enrolled,
    new_totp_secret,
    verify_backup_key,
    verify_login,
    verify_totp,
)
from .config_manager import (
    add_host,
    delete_host,
    get_available_ssh_keys,
    get_dockerhub_config,
    get_email_config,
    get_homeassistant_config,
    get_hosts,
    get_opnsense_config,
    get_pbs_config,
    get_pfsense_config,
    get_portainer_config,
    get_proxmox_config,
    get_pushover_config,
    get_ssh_config,
    get_timezone,
    get_update_check_schedule,
    save_dockerhub_config,
    save_email_config,
    save_homeassistant_config,
    save_opnsense_config,
    save_pbs_config,
    save_pfsense_config,
    save_portainer_config,
    save_proxmox_config,
    save_pushover_config,
    save_timezone,
    save_update_check_schedule,
    save_wizard_container_selection,
    set_host_auto_update,
)
from .credentials import (
    delete_credentials,
    get_integration_credentials,
    save_credentials,
    save_integration_credentials,
)
from .proxmox_client import ProxmoxClient
from .ssh_client import detect_docker_stacks, discover_containers, verify_connection
from .backend_loader import reload_backends
from .templates_env import make_templates

router = APIRouter()
templates = make_templates()


def _ssh_section_ctx(request: Request, **extra) -> dict:
    """Build the context dict for partials/setup_ssh_section.html."""
    proxmox_pending = request.session.get("setup_proxmox_pending", [])
    integration_pending = request.session.get("setup_integration_pending", [])
    return {
        "request": request,
        "hosts": get_hosts(),
        "available_keys": get_available_ssh_keys(),
        "proxmox_pending": integration_pending + proxmox_pending,
        **extra,
    }


def _queue_integration_host(request: Request, integration_key: str, name: str, url: str) -> None:
    """Queue the integration host itself as a pending SSH host in the wizard."""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or "" if url else ""
    pending: list[dict] = request.session.get("setup_integration_pending", [])
    # Replace any existing entry for this integration
    pending = [h for h in pending if h.get("integration") != integration_key]
    if url and hostname:
        pending.append({
            "name": name,
            "ip": hostname,
            "type": "integration",
            "node": "",
            "integration": integration_key,
        })
    request.session["setup_integration_pending"] = pending


def _timezone_groups() -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for zone in sorted(available_timezones()):
        region = zone.split("/")[0]
        groups.setdefault(region, []).append(zone)
    return sorted(groups.items())


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, resets on container restart)
# ---------------------------------------------------------------------------

_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60  # 15 minutes


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Returns (allowed, seconds_remaining). Clears expired attempts."""
    now = time.time()
    _ATTEMPTS[ip] = [t for t in _ATTEMPTS[ip] if now - t < _LOCKOUT_SECONDS]
    if len(_ATTEMPTS[ip]) >= _MAX_ATTEMPTS:
        remaining = int(_LOCKOUT_SECONDS - (now - _ATTEMPTS[ip][0]))
        return False, remaining
    return True, 0


def _record_failure(ip: str) -> None:
    _ATTEMPTS[ip].append(time.time())


def _clear_attempts(ip: str) -> None:
    _ATTEMPTS.pop(ip, None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else request.client.host


# ---------------------------------------------------------------------------
# Setup — screen 1: welcome + timezone
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_welcome(request: Request) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "setup_welcome.html",
        {
            "request": request,
            "timezone_groups": _timezone_groups(),
            "current_tz": get_timezone(),
        },
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_welcome_submit(
    request: Request,
    timezone: str = Form("UTC"),
) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/login", status_code=302)
    save_timezone(timezone)
    return RedirectResponse("/setup/account", status_code=303)


# ---------------------------------------------------------------------------
# Setup — screen 2: account credentials
# ---------------------------------------------------------------------------


@router.get("/setup/account", response_class=HTMLResponse)
async def setup_account(request: Request) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("setup_account.html", {"request": request})


@router.post("/setup/account", response_class=HTMLResponse)
async def setup_account_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/login", status_code=302)

    errors: list[str] = []
    username = username.strip()

    if len(username) < 2:
        errors.append("Username must be at least 2 characters.")
    elif not re.match(r"^[a-zA-Z0-9_-]+$", username):
        errors.append(
            "Username may only contain letters, numbers, hyphens, and underscores."
        )

    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != password_confirm:
        errors.append("Passwords do not match.")

    if errors:
        return templates.TemplateResponse(
            "setup_account.html",
            {
                "request": request,
                "errors": errors,
                "username_value": username,
            },
        )

    backup_key = create_admin(username=username, password=password, totp_secret=None)
    request.session["setup_account_done"] = True
    request.session["setup_backup_key"] = backup_key
    return RedirectResponse("/setup/security", status_code=303)


# ---------------------------------------------------------------------------
# Setup — screen 3: two-factor authentication
# ---------------------------------------------------------------------------


@router.get("/setup/security", response_class=HTMLResponse)
async def setup_security(request: Request) -> HTMLResponse:
    if not request.session.get("setup_account_done"):
        return RedirectResponse("/login", status_code=302)
    secret = new_totp_secret()
    request.session["setup_totp_secret"] = secret
    return templates.TemplateResponse(
        "setup_security.html",
        {
            "request": request,
            "totp_uri": get_totp_uri(secret),
            "totp_secret": secret,
        },
    )


@router.post("/setup/security", response_class=HTMLResponse)
async def setup_security_submit(
    request: Request,
    enable_mfa: str = Form(""),
    totp_code: str = Form(""),
) -> HTMLResponse:
    if not request.session.get("setup_account_done"):
        return RedirectResponse("/login", status_code=302)

    if enable_mfa == "on":
        session_secret = request.session.get("setup_totp_secret", "")
        if not session_secret or not pyotp.TOTP(session_secret).verify(
            totp_code.strip(), valid_window=1
        ):
            secret = session_secret or new_totp_secret()
            request.session["setup_totp_secret"] = secret
            return templates.TemplateResponse(
                "setup_security.html",
                {
                    "request": request,
                    "totp_uri": get_totp_uri(secret),
                    "totp_secret": secret,
                    "errors": [
                        "Authenticator code is incorrect. Make sure your phone's time is synced and try again."
                    ],
                    "enable_mfa_checked": True,
                },
            )
        enroll_mfa(session_secret)
        request.session.pop("setup_totp_secret", None)

    return RedirectResponse("/setup/recovery-code", status_code=303)


# ---------------------------------------------------------------------------
# Setup — screen 4: recovery code
# ---------------------------------------------------------------------------


@router.get("/setup/recovery-code", response_class=HTMLResponse)
async def setup_recovery_code(request: Request) -> HTMLResponse:
    backup_key = request.session.get("setup_backup_key")
    if not backup_key:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "setup_recovery.html",
        {
            "request": request,
            "backup_key": backup_key,
        },
    )


@router.post("/setup/recovery-code/confirm", response_class=HTMLResponse)
async def setup_recovery_code_confirm(request: Request) -> HTMLResponse:
    request.session.pop("setup_backup_key", None)
    request.session.pop("setup_account_done", None)
    return RedirectResponse("/setup/connect", status_code=303)


# ---------------------------------------------------------------------------
# Setup — screen 5: connect integrations
# ---------------------------------------------------------------------------


@router.get("/setup/connect", response_class=HTMLResponse)
async def setup_connect(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)

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
    dh_cfg = get_integration_credentials("dockerhub")

    return templates.TemplateResponse(
        "setup_connect.html",
        {
            "request": request,
            "proxmox_url": px_cfg.get("url", ""),
            "proxmox_connected": bool(px_cfg.get("url") and (px_creds.get("secret") or px_creds.get("api_token"))),
            "pbs_url": pbs_cfg.get("url", ""),
            "pbs_connected": bool(pbs_cfg.get("url") and (pbs_creds.get("secret") or pbs_creds.get("api_token"))),
            "opnsense_url": opn_cfg.get("url", ""),
            "opnsense_connected": bool(opn_cfg.get("url") and opn_creds.get("api_key")),
            "pfsense_url": pf_cfg.get("url", ""),
            "pfsense_connected": bool(pf_cfg.get("url") and pf_creds.get("api_key")),
            "homeassistant_url": ha_cfg.get("url", ""),
            "homeassistant_connected": bool(
                ha_cfg.get("url") and ha_creds.get("token")
            ),
            "portainer_url": port_cfg.get("url", ""),
            "portainer_connected": bool(
                port_cfg.get("url") and port_creds.get("api_key")
            ),
            "dockerhub_connected": bool(dh_cfg.get("token")),
        },
    )


# --- Proxmox ---


@router.post("/setup/connect/proxmox/test", response_class=HTMLResponse)
async def setup_test_proxmox(
    request: Request,
    proxmox_url: str = Form(""),
    proxmox_api_user: str = Form(""),
    proxmox_token_id: str = Form(""),
    proxmox_secret: str = Form(""),
    proxmox_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = proxmox_url.strip().rstrip("/")
    api_user = proxmox_api_user.strip()
    token_id = proxmox_token_id.strip()
    secret = proxmox_secret.strip()
    verify_ssl = proxmox_verify_ssl == "on"
    if not url or not token_id or not secret:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter a URL, Token ID, and Secret first.</span>'
        )
    token = f"{token_id}={secret}"
    try:
        client = ProxmoxClient(url=url, api_token=token, verify_ssl=verify_ssl)
        version = await client.get_version()
        ver = version.get("version", "")
        return HTMLResponse(
            f'<span class="text-green-400 text-sm">&#10003; Connected — Proxmox VE {ver}. Click Save to continue.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "403" in msg:
            hint = "Invalid API token."
        elif "connect" in msg.lower() or "Name or service" in msg:
            hint = "Can&#39;t reach that address — check the URL."
        elif "SSL" in msg or "certificate" in msg.lower():
            hint = "SSL error — try disabling SSL verification."
        else:
            hint = msg[:120]
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/connect/proxmox/save", response_class=HTMLResponse)
async def setup_save_proxmox(
    request: Request,
    proxmox_url: str = Form(""),
    proxmox_api_user: str = Form(""),
    proxmox_token_id: str = Form(""),
    proxmox_secret: str = Form(""),
    proxmox_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = proxmox_url.strip().rstrip("/")
    api_user = proxmox_api_user.strip()
    token_id = proxmox_token_id.strip()
    secret = proxmox_secret.strip()
    verify_ssl = proxmox_verify_ssl == "on"
    save_proxmox_config(url=url, verify_ssl=verify_ssl)
    if api_user or token_id or secret:
        save_integration_credentials(
            "proxmox",
            api_user=api_user or None,
            token_id=token_id or None,
            secret=secret or None,
        )
    _queue_integration_host(request, "proxmox", "Proxmox VE", url)
    return templates.TemplateResponse(
        "partials/setup_proxmox_section.html",
        {
            "request": request,
            "proxmox_url": url,
            "proxmox_connected": True,
            "proxmox_saved": True,
        },
    )


@router.post("/setup/connect/proxmox/discover", response_class=HTMLResponse)
async def setup_proxmox_discover(request: Request) -> HTMLResponse:
    cfg = get_proxmox_config()
    creds = get_integration_credentials("proxmox")
    url = cfg.get("url", "")
    token_id = creds.get("token_id", "")
    secret = creds.get("secret", "")
    # Legacy fallback: old format stored full token in api_token
    if not token_id:
        api_user = creds.get("api_user", "")
        api_token = creds.get("api_token", "")
        token = f"{api_user}!{api_token}" if api_user else api_token
    else:
        token = f"{token_id}={secret}"
    verify_ssl = cfg.get("verify_ssl", False)
    if not url or not token:
        return HTMLResponse(
            '<p class="text-sm text-red-400">Proxmox not configured.</p>'
        )
    try:
        client = ProxmoxClient(url=url, api_token=token, verify_ssl=verify_ssl)
        resources = await client.discover_resources()
        return templates.TemplateResponse(
            "partials/setup_proxmox_section.html",
            {
                "request": request,
                "proxmox_url": url,
                "proxmox_connected": True,
                "proxmox_resources": resources,
            },
        )
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-sm text-red-400">Discovery failed: {exc}</p>'
        )


@router.post("/setup/connect/proxmox/select-hosts", response_class=HTMLResponse)
async def setup_proxmox_select_hosts(request: Request) -> HTMLResponse:
    form = await request.form()
    selected = form.getlist("selected_hosts")  # list of "node:vmid:type:name:ip" strings
    pending = []
    for entry in selected:
        parts = entry.split(":", 4)
        if len(parts) >= 4:
            pending.append(
                {
                    "node": parts[0],
                    "vmid": parts[1],
                    "type": parts[2],
                    "name": parts[3],
                    "ip": parts[4] if len(parts) > 4 else "",
                }
            )
    request.session["setup_proxmox_pending"] = pending
    count = len(pending)
    label = f"{count} host{'s' if count != 1 else ''}"
    return HTMLResponse(
        f'<p class="text-sm text-green-400">&#10003; {label} queued for SSH setup in the next step.</p>'
    )


# --- Proxmox Backup Server ---


@router.post("/setup/connect/pbs/test", response_class=HTMLResponse)
async def setup_test_pbs(
    request: Request,
    pbs_url: str = Form(""),
    pbs_api_user: str = Form(""),
    pbs_token_id: str = Form(""),
    pbs_secret: str = Form(""),
    pbs_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = pbs_url.strip().rstrip("/")
    token_id = pbs_token_id.strip()
    secret = pbs_secret.strip()
    verify_ssl = pbs_verify_ssl == "on"
    if not url or not token_id or not secret:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter a URL, Token ID, and Secret first.</span>'
        )
    # PBS auth: PBSAPIToken=<tokenid>:<secret>  (colon separator, not equals)
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=10) as c:
            resp = await c.get(
                f"{url}/api2/json/version",
                headers={"Authorization": f"PBSAPIToken={token_id}:{secret}"},
            )
            resp.raise_for_status()
            ver = resp.json().get("data", {}).get("version", "")
        import logging
        logging.getLogger(__name__).info("PBS connected: version %s", ver)
        return HTMLResponse(
            f'<span class="text-green-400 text-sm">&#10003; Connected — Proxmox Backup Server {ver}. Click Save to continue.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        import logging
        logging.getLogger(__name__).warning("PBS connection failed: %s", msg)
        hint = "Invalid Token ID or Secret." if ("401" in msg or "403" in msg) else msg[:120]
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/connect/pbs/save", response_class=HTMLResponse)
async def setup_save_pbs(
    request: Request,
    pbs_url: str = Form(""),
    pbs_api_user: str = Form(""),
    pbs_token_id: str = Form(""),
    pbs_secret: str = Form(""),
    pbs_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = pbs_url.strip().rstrip("/")
    api_user = pbs_api_user.strip()
    token_id = pbs_token_id.strip()
    secret = pbs_secret.strip()
    verify_ssl = pbs_verify_ssl == "on"
    save_pbs_config(url=url, verify_ssl=verify_ssl)
    if api_user or token_id or secret:
        save_integration_credentials(
            "proxmox_backup",
            api_user=api_user or None,
            token_id=token_id or None,
            secret=secret or None,
        )
    _queue_integration_host(request, "proxmox_backup", "Proxmox Backup Server", url)
    return HTMLResponse(
        '<p class="text-sm text-green-400">&#10003; Proxmox Backup Server saved.</p>'
    )


# --- OPNsense ---


@router.post("/setup/connect/opnsense/test", response_class=HTMLResponse)
async def setup_test_opnsense(
    request: Request,
    opnsense_url: str = Form(""),
    opnsense_api_key: str = Form(""),
    opnsense_api_secret: str = Form(""),
    opnsense_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = opnsense_url.strip().rstrip("/")
    key = opnsense_api_key.strip()
    secret = opnsense_api_secret.strip()
    verify_ssl = opnsense_verify_ssl == "on"
    if not url or not key or not secret:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter URL, API key, and API secret first.</span>'
        )
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=10) as c:
            resp = await c.get(
                f"{url}/api/core/firmware/info",
                auth=(key, secret),
            )
            resp.raise_for_status()
        return HTMLResponse(
            '<span class="text-green-400 text-sm">&#10003; Connected. Click Save to continue.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        hint = (
            "Invalid API key or secret."
            if ("401" in msg or "403" in msg)
            else msg[:120]
        )
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/connect/opnsense/save", response_class=HTMLResponse)
async def setup_save_opnsense(
    request: Request,
    opnsense_url: str = Form(""),
    opnsense_api_key: str = Form(""),
    opnsense_api_secret: str = Form(""),
    opnsense_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = opnsense_url.strip().rstrip("/")
    key = opnsense_api_key.strip()
    secret = opnsense_api_secret.strip()
    verify_ssl = opnsense_verify_ssl == "on"
    save_opnsense_config(url=url, verify_ssl=verify_ssl)
    if key and secret:
        save_integration_credentials("opnsense", api_key=key, api_secret=secret)
    _queue_integration_host(request, "opnsense", "OPNsense", url)
    return HTMLResponse(
        '<p class="text-sm text-green-400">&#10003; OPNsense saved.</p>'
    )


# --- pfSense ---


@router.post("/setup/connect/pfsense/test", response_class=HTMLResponse)
async def setup_test_pfsense(
    request: Request,
    pfsense_url: str = Form(""),
    pfsense_api_key: str = Form(""),
    pfsense_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = pfsense_url.strip().rstrip("/")
    key = pfsense_api_key.strip()
    verify_ssl = pfsense_verify_ssl == "on"
    if not url or not key:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter a URL and API key first.</span>'
        )
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=10) as c:
            resp = await c.get(
                f"{url}/api/v1/system/version",
                headers={"Authorization": key},
            )
            resp.raise_for_status()
        return HTMLResponse(
            '<span class="text-green-400 text-sm">&#10003; Connected. Click Save to continue.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        hint = "Invalid API key." if ("401" in msg or "403" in msg) else msg[:120]
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/connect/pfsense/save", response_class=HTMLResponse)
async def setup_save_pfsense(
    request: Request,
    pfsense_url: str = Form(""),
    pfsense_api_key: str = Form(""),
    pfsense_verify_ssl: str = Form(""),
) -> HTMLResponse:
    url = pfsense_url.strip().rstrip("/")
    key = pfsense_api_key.strip()
    verify_ssl = pfsense_verify_ssl == "on"
    save_pfsense_config(url=url, verify_ssl=verify_ssl)
    if key:
        save_integration_credentials("pfsense", api_key=key)
    _queue_integration_host(request, "pfsense", "pfSense", url)
    return HTMLResponse('<p class="text-sm text-green-400">&#10003; pfSense saved.</p>')


# --- Home Assistant ---


@router.post("/setup/connect/homeassistant/test", response_class=HTMLResponse)
async def setup_test_homeassistant(
    request: Request,
    ha_url: str = Form(""),
    ha_token: str = Form(""),
) -> HTMLResponse:
    url = ha_url.strip().rstrip("/")
    token = ha_token.strip()
    if not url or not token:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter a URL and access token first.</span>'
        )
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            resp = await c.get(
                f"{url}/api/",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            ver = resp.json().get("version", "")
        msg = f"Connected — Home Assistant {ver}." if ver else "Connected."
        return HTMLResponse(
            f'<span class="text-green-400 text-sm">&#10003; {msg} Click Save to continue.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        hint = "Invalid access token." if ("401" in msg or "403" in msg) else msg[:120]
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/connect/homeassistant/save", response_class=HTMLResponse)
async def setup_save_homeassistant(
    request: Request,
    ha_url: str = Form(""),
    ha_token: str = Form(""),
) -> HTMLResponse:
    url = ha_url.strip().rstrip("/")
    token = ha_token.strip()
    save_homeassistant_config(url=url)
    if token:
        save_integration_credentials("homeassistant", token=token)
    _queue_integration_host(request, "homeassistant", "Home Assistant", url)
    return HTMLResponse(
        '<p class="text-sm text-green-400">&#10003; Home Assistant saved. '
        '<span class="text-slate-400">Note: Keepup can alert you to updates but cannot install them automatically.</span></p>'
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "needs_mfa": mfa_enrolled(),
            "needs_username": bool(
                get_integration_credentials("admin").get("username")
            ),
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    totp_code: str = Form(""),
    remember_me: str = Form(""),
) -> HTMLResponse:
    ip = _client_ip(request)
    allowed, remaining = _check_rate_limit(ip)
    needs_username = bool(get_integration_credentials("admin").get("username"))

    if not allowed:
        mins = remaining // 60 + 1
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "needs_mfa": mfa_enrolled(),
                "needs_username": needs_username,
                "error": f"Too many failed attempts. Try again in {mins} minute{'s' if mins != 1 else ''}.",
            },
        )

    ok = verify_login(username, password)
    if ok and mfa_enrolled():
        ok = verify_totp(totp_code)

    if not ok:
        _record_failure(ip)
        allowed, remaining = _check_rate_limit(ip)
        attempts_left = _MAX_ATTEMPTS - len(_ATTEMPTS[ip])
        if not allowed:
            error = "Too many failed attempts. Account locked for 15 minutes."
        elif attempts_left <= 2:
            error = f"Incorrect credentials. {attempts_left} attempt{'s' if attempts_left != 1 else ''} remaining before lockout."
        else:
            error = "Incorrect username, password, or authenticator code."
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "needs_mfa": mfa_enrolled(),
                "needs_username": needs_username,
                "error": error,
            },
        )

    _clear_attempts(ip)
    request.session["authenticated"] = True
    if remember_me == "on":
        request.session["remember_me"] = True

    next_url = request.query_params.get("next", "/home")
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "step": "key",
        },
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    backup_key: str = Form(""),
) -> HTMLResponse:
    if not verify_backup_key(backup_key):
        return templates.TemplateResponse(
            "forgot_password.html",
            {
                "request": request,
                "step": "key",
                "error": "That backup key is not correct.",
            },
        )
    request.session["recovery_verified"] = True
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "step": "reset",
        },
    )


@router.post("/forgot-password/reset", response_class=HTMLResponse)
async def forgot_password_reset(
    request: Request,
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
) -> HTMLResponse:
    if not request.session.get("recovery_verified"):
        return RedirectResponse("/forgot-password", status_code=302)

    errors: list[str] = []
    if len(new_password) < 8:
        errors.append("Password must be at least 8 characters.")
    if new_password != new_password_confirm:
        errors.append("Passwords do not match.")

    if errors:
        return templates.TemplateResponse(
            "forgot_password.html",
            {
                "request": request,
                "step": "reset",
                "errors": errors,
            },
        )

    from .auth import change_password

    change_password(new_password)
    request.session.pop("recovery_verified", None)

    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "step": "done",
        },
    )


# ---------------------------------------------------------------------------
# Setup — SSH hosts (Screen 6)
# ---------------------------------------------------------------------------


@router.get("/setup/hosts", response_class=HTMLResponse)
async def setup_hosts_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    proxmox_pending = request.session.get("setup_proxmox_pending", [])
    integration_pending = request.session.get("setup_integration_pending", [])
    return templates.TemplateResponse(
        "setup_hosts.html",
        {
            "request": request,
            "hosts": get_hosts(),
            "available_keys": get_available_ssh_keys(),
            "proxmox_pending": integration_pending + proxmox_pending,
        },
    )


@router.post("/setup/hosts/add", response_class=HTMLResponse)
async def setup_add_host(
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    user: str = Form(""),
    port: str = Form(""),
    auth_method: str = Form("password"),
    ssh_password: str = Form(""),
    key_file: str = Form(""),
    enable_auto_update: str = Form(""),
) -> HTMLResponse:
    name = name.strip()
    host_addr = host.strip()
    user_val = user.strip() or None
    port_val = int(port) if port.strip().isdigit() else None
    key_path = f"/app/keys/{key_file}" if auth_method == "key" and key_file else None
    auto_update = enable_auto_update == "on"

    if not name or not host_addr:
        return templates.TemplateResponse(
            "partials/setup_ssh_section.html",
            _ssh_section_ctx(
                request,
                add_error="Name and host/IP are required.",
                form={
                    "name": name,
                    "host": host_addr,
                    "user": user_val or "",
                    "port": port or "",
                    "auth_method": auth_method,
                    "key_file": key_file,
                },
            ),
        )

    host_entry: dict = {"name": name, "host": host_addr}
    if user_val:
        host_entry["user"] = user_val
    if port_val:
        host_entry["port"] = port_val
    if key_path:
        host_entry["key"] = key_path

    creds: dict = {}
    if auth_method == "password" and ssh_password.strip():
        creds = {"ssh_password": ssh_password.strip()}

    result = await verify_connection(host_entry, get_ssh_config(), creds)
    if not result["ok"]:
        return templates.TemplateResponse(
            "partials/setup_ssh_section.html",
            _ssh_section_ctx(
                request,
                add_error=f"Could not connect: {result['message']}",
                form={
                    "name": name,
                    "host": host_addr,
                    "user": user_val or "",
                    "port": port or "",
                    "auth_method": auth_method,
                    "key_file": key_file,
                },
            ),
        )

    # Connection succeeded — check for Docker before committing
    stack_count = await detect_docker_stacks(host_entry, get_ssh_config(), creds)

    if stack_count > 0:
        # Store pending host in session (avoids putting credentials in HTML hidden fields)
        label = f"{stack_count} stack{'s' if stack_count != 1 else ''}"
        request.session["pending_ssh_host"] = {
            "name": name,
            "host": host_addr,
            "user": user_val or "",
            "port": port,
            "auth_method": auth_method,
            "ssh_password": creds.get("ssh_password", ""),
            "key_file": key_file,
            "auto_update": auto_update,
        }
        return templates.TemplateResponse(
            "partials/setup_ssh_section.html",
            _ssh_section_ctx(
                request,
                docker_prompt={"name": name, "stack_label": label},
            ),
        )

    # No Docker — add host directly
    slug = add_host(
        name=name, host=host_addr, user=user_val, port=port_val, key_path=key_path
    )
    if auth_method == "password" and ssh_password.strip():
        save_credentials(slug, ssh_password=ssh_password.strip())
    if auto_update:
        set_host_auto_update(
            slug, os_enabled=True, os_schedule="weekly", auto_reboot=False
        )

    return templates.TemplateResponse(
        "partials/setup_ssh_section.html",
        _ssh_section_ctx(
            request,
            add_success=f"{name} added successfully.",
        ),
    )


@router.post("/setup/hosts/confirm-add", response_class=HTMLResponse)
async def setup_confirm_add_host(
    request: Request,
    enable_docker: str = Form("no"),
) -> HTMLResponse:
    pending = request.session.pop("pending_ssh_host", None)
    if not pending:
        return templates.TemplateResponse(
            "partials/setup_ssh_section.html",
            _ssh_section_ctx(
                request,
                add_error="Session expired — please add the host again.",
            ),
        )

    name = pending["name"]
    host_addr = pending["host"]
    user_val = pending["user"] or None
    port_str = pending.get("port", "")
    port_val = int(port_str) if str(port_str).strip().isdigit() else None
    auth_method = pending["auth_method"]
    ssh_password = pending.get("ssh_password", "")
    key_file = pending.get("key_file", "")
    auto_update = pending.get("auto_update", False)
    key_path = f"/app/keys/{key_file}" if auth_method == "key" and key_file else None
    docker_mode = "all" if enable_docker == "yes" else None

    slug = add_host(
        name=name,
        host=host_addr,
        user=user_val,
        port=port_val,
        key_path=key_path,
        docker_mode=docker_mode,
    )
    if auth_method == "password" and ssh_password:
        save_credentials(slug, ssh_password=ssh_password)
    if auto_update:
        set_host_auto_update(
            slug, os_enabled=True, os_schedule="weekly", auto_reboot=False
        )

    return templates.TemplateResponse(
        "partials/setup_ssh_section.html",
        _ssh_section_ctx(
            request,
            add_success=f"{name} added{' with container monitoring' if docker_mode else ''} successfully.",
        ),
    )


@router.post("/setup/hosts/{slug}/remove", response_class=HTMLResponse)
async def setup_remove_host(request: Request, slug: str) -> HTMLResponse:
    delete_host(slug)
    delete_credentials(slug)
    return templates.TemplateResponse(
        "partials/setup_ssh_section.html", _ssh_section_ctx(request)
    )


@router.post("/setup/hosts/card-test", response_class=HTMLResponse)
async def setup_card_test(
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    user: str = Form("root"),
    port: str = Form("22"),
    auth_method: str = Form("key"),
    ssh_password: str = Form(""),
    card_index: str = Form("1"),
) -> HTMLResponse:
    try:
        idx = int(card_index)
    except ValueError:
        idx = 1
    host_entry: dict = {"name": name.strip() or "test", "host": host.strip()}
    if user.strip():
        host_entry["user"] = user.strip()
    if port.strip().isdigit():
        host_entry["port"] = int(port)
    creds: dict = {}
    if auth_method == "password" and ssh_password.strip():
        creds = {"ssh_password": ssh_password.strip()}
    try:
        result = await verify_connection(host_entry, get_ssh_config(), creds)
        if result["ok"]:
            return HTMLResponse(
                f'<span class="w-1.5 h-1.5 rounded-full bg-green-400 flex-shrink-0" id="dot-{idx}"></span>'
            )
        return HTMLResponse(
            f'<span class="w-1.5 h-1.5 rounded-full bg-red-400 flex-shrink-0" id="dot-{idx}" title="Failed"></span>'
        )
    except Exception:
        return HTMLResponse(
            f'<span class="w-1.5 h-1.5 rounded-full bg-red-400 flex-shrink-0" id="dot-{idx}" title="Error"></span>'
        )


@router.post("/setup/hosts/card-add", response_class=HTMLResponse)
async def setup_card_add(
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    user: str = Form("root"),
    port: str = Form("22"),
    auth_method: str = Form("key"),
    ssh_password: str = Form(""),
    card_index: str = Form("1"),
    node: str = Form(""),
    host_type: str = Form(""),
) -> HTMLResponse:
    try:
        idx = int(card_index)
    except ValueError:
        idx = 1
    name = name.strip()
    host_addr = host.strip()
    if not name or not host_addr:
        return templates.TemplateResponse(
            "partials/setup_host_card_confirmed.html",
            {
                "request": request,
                "card_index": idx,
                "name": name or "(unnamed)",
                "host_addr": host_addr,
                "node": node,
                "host_type": host_type,
                "error": "Display name and IP address are required.",
            },
        )
    user_val = user.strip() or None
    port_val = int(port) if port.strip().isdigit() else None
    slug = add_host(name=name, host=host_addr, user=user_val, port=port_val)
    if auth_method == "password" and ssh_password.strip():
        save_credentials(slug, ssh_password=ssh_password.strip())
    return templates.TemplateResponse(
        "partials/setup_host_card_confirmed.html",
        {
            "request": request,
            "card_index": idx,
            "name": name,
            "host_addr": host_addr,
            "node": node,
            "host_type": host_type,
            "error": None,
        },
    )


@router.post("/setup/portainer/test", response_class=HTMLResponse)
async def setup_test_portainer(
    request: Request,
    portainer_url: str = Form(""),
    portainer_api_key: str = Form(""),
    portainer_verify_ssl: str = Form(""),
) -> HTMLResponse:
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
            f'<span class="text-green-400 text-sm">&#10003; Connected — {count} environment{"s" if count != 1 else ""} found. Click Save to apply.</span>'
        )
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "403" in msg:
            hint = "Invalid API token."
        elif "connect" in msg.lower() or "Name or service" in msg:
            hint = "Can&#39;t reach that address — check the URL."
        elif "SSL" in msg or "certificate" in msg.lower():
            hint = "SSL error — try disabling SSL verification."
        else:
            hint = msg[:120]
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {hint}</span>'
        )


@router.post("/setup/portainer/save", response_class=HTMLResponse)
async def setup_save_portainer(
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
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    portainer_connected = bool(port_cfg.get("url") and port_creds.get("api_key"))
    return templates.TemplateResponse(
        "partials/setup_portainer_section.html",
        {
            "request": request,
            "portainer_url": port_cfg.get("url", ""),
            "portainer_connected": portainer_connected,
            "portainer_saved": True,
        },
    )


@router.post("/setup/dockerhub/save", response_class=HTMLResponse)
async def setup_save_dockerhub(
    request: Request,
    dockerhub_username: str = Form(""),
    dockerhub_token: str = Form(""),
) -> HTMLResponse:
    username = dockerhub_username.strip()
    token = dockerhub_token.strip()
    save_dockerhub_config(username=username)
    if token:
        save_integration_credentials("dockerhub", token=token)
    await reload_backends()
    return HTMLResponse(
        '<p class="text-sm text-green-400">&#10003; DockerHub credentials saved.</p>'
    )


@router.post("/setup/finish")
async def setup_finish(request: Request):
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Setup — Summary (Screen 8)
# ---------------------------------------------------------------------------


@router.get("/setup/summary", response_class=HTMLResponse)
async def setup_summary_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)

    # Integrations connected status
    def _cred_set(name: str, *keys: str) -> bool:
        creds = get_integration_credentials(name)
        return all(creds.get(k) for k in keys)

    integrations = [
        (
            "Proxmox VE",
            get_proxmox_config().get("url"),
            _cred_set("proxmox", "secret"),
        ),
        ("Proxmox Backup", get_pbs_config().get("url"), _cred_set("proxmox_backup", "secret")),
        (
            "OPNsense",
            get_opnsense_config().get("url"),
            _cred_set("opnsense", "api_key", "api_secret"),
        ),
        ("pfSense", get_pfsense_config().get("url"), _cred_set("pfsense", "api_key")),
        (
            "Home Assistant",
            get_homeassistant_config().get("url"),
            _cred_set("homeassistant", "api_token"),
        ),
        (
            "Portainer",
            get_portainer_config().get("url"),
            _cred_set("portainer", "api_key"),
        ),
        ("DockerHub", get_dockerhub_config().get("username"), False),
    ]
    # Only show integrations that have a URL/username configured
    configured_integrations = [
        (name, bool(url or cred)) for name, url, cred in integrations if url
    ]
    dockerhub_cfg = get_dockerhub_config()
    if dockerhub_cfg.get("username"):
        configured_integrations.append(("DockerHub", True))

    pushover_cfg = get_pushover_config()
    pushover_creds = get_integration_credentials("pushover")
    schedule_labels = {
        "6h": "every 6 hours",
        "12h": "every 12 hours",
        "24h": "daily",
        "manual": "manual only",
    }

    return templates.TemplateResponse(
        "setup_summary.html",
        {
            "request": request,
            "timezone": get_timezone(),
            "mfa_enabled": mfa_enrolled(),
            "hosts": get_hosts(),
            "configured_integrations": configured_integrations,
            "pushover_enabled": pushover_cfg.get("enabled", False)
            and bool(pushover_creds.get("api_token")),
            "update_schedule_label": schedule_labels.get(
                get_update_check_schedule(), "manual only"
            ),
        },
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Setup — Container monitoring (Screen 7)
# ---------------------------------------------------------------------------


@router.get("/setup/containers", response_class=HTMLResponse)
async def setup_containers_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    hosts = get_hosts()
    ssh_cfg = get_ssh_config()

    host_data = []
    for h in hosts:
        from .credentials import get_credentials

        creds = get_credentials(h["slug"])
        containers = await discover_containers(h, ssh_cfg, creds)
        host_data.append(
            {
                "id": h["slug"],
                "name": h["name"],
                "ip": h.get("host", ""),
                "containers": containers,
            }
        )

    return templates.TemplateResponse(
        "setup_containers.html",
        {
            "request": request,
            "hosts": host_data,
        },
    )


@router.post("/setup/containers/save", response_class=HTMLResponse)
async def setup_containers_save(request: Request) -> HTMLResponse:
    form = await request.form()
    containers = form.getlist("containers")
    save_wizard_container_selection(containers)
    return RedirectResponse("/setup/notifications", status_code=303)


# ---------------------------------------------------------------------------
# Setup — Notifications + update check schedule (Screen 8)
# ---------------------------------------------------------------------------


@router.get("/setup/notifications", response_class=HTMLResponse)
async def setup_notifications_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    pushover_creds = get_integration_credentials("pushover")
    pushover_cfg = get_pushover_config()
    email_cfg = get_email_config()
    email_creds = get_integration_credentials("email")
    return templates.TemplateResponse(
        "setup_notifications.html",
        {
            "request": request,
            "pushover_token_set": bool(pushover_creds.get("api_token")),
            "pushover_user_set": bool(pushover_creds.get("user_key")),
            "pushover_enabled": pushover_cfg.get("enabled", False),
            "email_configured": bool(email_cfg.get("smtp_host")),
            "email_sender": email_cfg.get("sender_address", ""),
            "email_recipient": email_cfg.get("recipient_address", ""),
            "email_smtp_host": email_cfg.get("smtp_host", ""),
            "email_smtp_port": email_cfg.get("smtp_port", 587),
            "email_tls": email_cfg.get("tls", True),
            "email_sender_name": email_cfg.get("sender_name", ""),
            "email_password_set": bool(email_creds.get("smtp_password")),
            "update_schedule": get_update_check_schedule(),
        },
    )


@router.post("/setup/notifications/pushover/test", response_class=HTMLResponse)
async def setup_test_pushover(
    request: Request,
    pushover_token: str = Form(""),
    pushover_user_key: str = Form(""),
) -> HTMLResponse:
    token = pushover_token.strip()
    user_key = pushover_user_key.strip()
    if not token or not user_key:
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Enter an app token and user key first.</span>'
        )
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": token,
                    "user": user_key,
                    "title": "Keepup test",
                    "message": "Keepup setup — Pushover is connected.",
                },
            )
            if resp.status_code == 200:
                return HTMLResponse(
                    '<span class="text-green-400 text-sm">&#10003; Test notification sent.</span>'
                )
            body = resp.json()
            errors = ", ".join(body.get("errors", [str(resp.status_code)]))
            return HTMLResponse(
                f'<span class="text-red-400 text-sm">&#10007; {errors}</span>'
            )
    except Exception as exc:
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {str(exc)[:120]}</span>'
        )


@router.post("/setup/notifications/pushover/save", response_class=HTMLResponse)
async def setup_save_pushover(
    request: Request,
    pushover_token: str = Form(""),
    pushover_user_key: str = Form(""),
    pushover_enabled: str = Form(""),
) -> HTMLResponse:
    token = pushover_token.strip()
    user_key = pushover_user_key.strip()
    enabled = pushover_enabled == "on"
    if token:
        save_integration_credentials("pushover", api_token=token, user_key=user_key)
    save_pushover_config(enabled=enabled)
    return HTMLResponse(
        '<span class="text-green-400 text-sm">&#10003; Pushover settings saved.</span>'
    )


@router.post("/setup/notifications/schedule/save", response_class=HTMLResponse)
async def setup_save_schedule(
    request: Request,
    update_schedule: str = Form("manual"),
) -> HTMLResponse:
    save_update_check_schedule(update_schedule)
    labels = {
        "6h": "every 6 hours",
        "12h": "every 12 hours",
        "24h": "daily",
        "manual": "manual only",
    }
    label = labels.get(update_schedule, "manual only")
    return HTMLResponse(
        f'<span class="text-green-400 text-sm">&#10003; Update checks set to {label}.</span>'
    )


@router.post("/setup/notifications/email/save", response_class=HTMLResponse)
async def setup_save_email(
    request: Request,
    sender_name: str = Form(""),
    sender_address: str = Form(""),
    recipient_address: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_password: str = Form(""),
    tls: str = Form(""),
) -> HTMLResponse:
    if not smtp_host.strip():
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">SMTP host is required.</span>'
        )
    save_email_config(
        sender_name=sender_name.strip(),
        sender_address=sender_address.strip(),
        recipient_address=recipient_address.strip(),
        smtp_host=smtp_host.strip(),
        smtp_port=smtp_port,
        tls=(tls == "on"),
    )
    if smtp_password.strip():
        save_integration_credentials("email", smtp_password=smtp_password.strip())
    return HTMLResponse(
        '<span class="text-green-400 text-sm">&#10003; Email settings saved.</span>'
    )


@router.post("/setup/notifications/email/test", response_class=HTMLResponse)
async def setup_test_email(
    request: Request,
    sender_name: str = Form(""),
    sender_address: str = Form(""),
    recipient_address: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_password: str = Form(""),
    tls: str = Form(""),
) -> HTMLResponse:
    import smtplib
    import ssl
    from email.mime.text import MIMEText

    host = smtp_host.strip()
    if not host or not sender_address.strip() or not recipient_address.strip():
        return HTMLResponse(
            '<span class="text-amber-400 text-sm">Fill in SMTP host, sender and recipient first.</span>'
        )

    # Fall back to saved password if none entered
    if not smtp_password.strip():
        creds = get_integration_credentials("email")
        smtp_password = creds.get("smtp_password", "")

    try:
        msg = MIMEText(
            "This is a test email from Keepup to verify your SMTP configuration."
        )
        msg["Subject"] = "Keepup test email"
        msg["From"] = (
            f"{sender_name.strip()} <{sender_address.strip()}>"
            if sender_name.strip()
            else sender_address.strip()
        )
        msg["To"] = recipient_address.strip()

        use_tls = tls == "on"
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                host, smtp_port, context=context, timeout=10
            ) as server:
                if smtp_password:
                    server.login(sender_address.strip(), smtp_password)
                server.sendmail(
                    sender_address.strip(), [recipient_address.strip()], msg.as_string()
                )
        else:
            with smtplib.SMTP(host, smtp_port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                if smtp_password:
                    server.login(sender_address.strip(), smtp_password)
                server.sendmail(
                    sender_address.strip(), [recipient_address.strip()], msg.as_string()
                )

        return HTMLResponse(
            '<span class="text-green-400 text-sm">&#10003; Test email sent.</span>'
        )
    except Exception as exc:
        return HTMLResponse(
            f'<span class="text-red-400 text-sm">&#10007; {str(exc)[:160]}</span>'
        )


@router.post("/setup/notifications/email/delete", response_class=HTMLResponse)
async def setup_delete_email(request: Request) -> HTMLResponse:
    from .config_manager import load_config, save_config

    config = load_config()
    config.pop("email", None)
    save_config(config)
    from .credentials import delete_credentials

    try:
        delete_credentials("email")
    except Exception:
        pass
    return HTMLResponse('<div class="config-section" id="config-email"></div>')
