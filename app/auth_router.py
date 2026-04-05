import time
from collections import defaultdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import (
    admin_exists,
    create_admin,
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
    get_hosts,
    get_portainer_config,
    get_ssh_config,
    save_dockerhub_config,
    save_portainer_config,
)
from .credentials import (
    delete_credentials,
    get_integration_credentials,
    save_credentials,
    save_integration_credentials,
)
from .ssh_client import detect_docker_stacks, verify_connection
from .backend_loader import reload_backends
from .templates_env import make_templates

router = APIRouter()
templates = make_templates()

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
# Setup
# ---------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/", status_code=302)
    # Generate a fresh TOTP secret each time the page loads
    secret = new_totp_secret()
    request.session["setup_totp_secret"] = secret
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "totp_uri": get_totp_uri(secret),
        "totp_secret": secret,
    })


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    totp_code: str = Form(""),
    enable_mfa: str = Form(""),
) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/", status_code=302)

    import re
    errors: list[str] = []
    username = username.strip()

    if len(username) < 2:
        errors.append("Username must be at least 2 characters.")
    elif not re.match(r'^[a-zA-Z0-9_-]+$', username):
        errors.append("Username may only contain letters, numbers, hyphens, and underscores.")

    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != password_confirm:
        errors.append("Passwords do not match.")

    totp_secret = None
    if enable_mfa == "on":
        session_secret = request.session.get("setup_totp_secret", "")
        if not session_secret:
            errors.append("Session expired — please refresh and try again.")
        else:
            import pyotp
            if not pyotp.TOTP(session_secret).verify(totp_code.strip(), valid_window=1):
                errors.append("Authenticator code is incorrect. Make sure your phone's time is synced and try again.")
            else:
                totp_secret = session_secret

    if errors:
        secret = request.session.get("setup_totp_secret", new_totp_secret())
        request.session["setup_totp_secret"] = secret
        return templates.TemplateResponse("setup.html", {
            "request": request,
            "totp_uri": get_totp_uri(secret),
            "totp_secret": secret,
            "errors": errors,
            "enable_mfa_checked": enable_mfa == "on",
            "username_value": username,
        })

    backup_key = create_admin(username=username, password=password, totp_secret=totp_secret)
    request.session.pop("setup_totp_secret", None)
    request.session["setup_backup_key"] = backup_key

    return RedirectResponse("/setup/backup-key", status_code=303)


@router.get("/setup/backup-key", response_class=HTMLResponse)
async def setup_backup_key(request: Request) -> HTMLResponse:
    backup_key = request.session.get("setup_backup_key")
    if not backup_key:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("setup_backup_key.html", {
        "request": request,
        "backup_key": backup_key,
    })


@router.post("/setup/backup-key/confirm", response_class=HTMLResponse)
async def setup_backup_key_confirm(request: Request) -> HTMLResponse:
    request.session.pop("setup_backup_key", None)
    return RedirectResponse("/setup/hosts", status_code=303)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "needs_mfa": mfa_enrolled(),
        "needs_username": bool(get_integration_credentials("admin").get("username")),
    })


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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "needs_mfa": mfa_enrolled(),
            "needs_username": needs_username,
            "error": f"Too many failed attempts. Try again in {mins} minute{'s' if mins != 1 else ''}.",
        })

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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "needs_mfa": mfa_enrolled(),
            "needs_username": needs_username,
            "error": error,
        })

    _clear_attempts(ip)
    request.session["authenticated"] = True
    if remember_me == "on":
        request.session["remember_me"] = True

    next_url = request.query_params.get("next", "/")
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
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "step": "key",
    })


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    backup_key: str = Form(""),
) -> HTMLResponse:
    if not verify_backup_key(backup_key):
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "step": "key",
            "error": "That backup key is not correct.",
        })
    request.session["recovery_verified"] = True
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "step": "reset",
    })


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
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "step": "reset",
            "errors": errors,
        })

    from .auth import change_password
    change_password(new_password)
    request.session.pop("recovery_verified", None)

    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "step": "done",
    })


# ---------------------------------------------------------------------------
# Setup — hosts & connections (step 3)
# ---------------------------------------------------------------------------

@router.get("/setup/hosts", response_class=HTMLResponse)
async def setup_hosts_page(request: Request) -> HTMLResponse:
    if not admin_exists():
        return RedirectResponse("/setup", status_code=302)
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    portainer_connected = bool(port_cfg.get("url") and port_creds.get("api_key"))
    return templates.TemplateResponse("setup_hosts.html", {
        "request": request,
        "hosts": get_hosts(),
        "available_keys": get_available_ssh_keys(),
        "portainer_url": port_cfg.get("url", ""),
        "portainer_connected": portainer_connected,
        "step": 3,
    })


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
) -> HTMLResponse:
    name = name.strip()
    host_addr = host.strip()
    user_val = user.strip() or None
    port_val = int(port) if port.strip().isdigit() else None
    key_path = f"/app/keys/{key_file}" if auth_method == "key" and key_file else None

    if not name or not host_addr:
        return templates.TemplateResponse("partials/setup_ssh_section.html", {
            "request": request,
            "hosts": get_hosts(),
            "available_keys": get_available_ssh_keys(),
            "add_error": "Name and host/IP are required.",
            "form": {"name": name, "host": host_addr, "user": user_val or "", "port": port or "", "auth_method": auth_method, "key_file": key_file},
        })

    host_entry = {"name": name, "host": host_addr}
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
        return templates.TemplateResponse("partials/setup_ssh_section.html", {
            "request": request,
            "hosts": get_hosts(),
            "available_keys": get_available_ssh_keys(),
            "add_error": f"Could not connect: {result['message']}",
            "form": {"name": name, "host": host_addr, "user": user_val or "", "port": port or "", "auth_method": auth_method, "key_file": key_file},
        })

    # Connection succeeded — check for Docker before committing
    stack_count = await detect_docker_stacks(host_entry, get_ssh_config(), creds)

    if stack_count > 0:
        # Docker found — ask user before adding
        label = f"{stack_count} stack{'s' if stack_count != 1 else ''}"
        return templates.TemplateResponse("partials/setup_ssh_section.html", {
            "request": request,
            "hosts": get_hosts(),
            "available_keys": get_available_ssh_keys(),
            "docker_prompt": {
                "name": name,
                "host": host_addr,
                "user": user_val or "",
                "port": port,
                "auth_method": auth_method,
                "ssh_password": ssh_password.strip(),
                "key_file": key_file,
                "stack_label": label,
            },
        })

    # No Docker (or detection failed) — add host directly
    slug = add_host(name=name, host=host_addr, user=user_val, port=port_val, key_path=key_path)
    if auth_method == "password" and ssh_password.strip():
        save_credentials(slug, ssh_password=ssh_password.strip())

    return templates.TemplateResponse("partials/setup_ssh_section.html", {
        "request": request,
        "hosts": get_hosts(),
        "available_keys": get_available_ssh_keys(),
        "add_success": f"{name} added successfully.",
    })


@router.post("/setup/hosts/confirm-add", response_class=HTMLResponse)
async def setup_confirm_add_host(
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    user: str = Form(""),
    port: str = Form(""),
    auth_method: str = Form("password"),
    ssh_password: str = Form(""),
    key_file: str = Form(""),
    enable_docker: str = Form("no"),
) -> HTMLResponse:
    name = name.strip()
    host_addr = host.strip()
    user_val = user.strip() or None
    port_val = int(port) if port.strip().isdigit() else None
    key_path = f"/app/keys/{key_file}" if auth_method == "key" and key_file else None
    docker_mode = "all" if enable_docker == "yes" else None

    slug = add_host(name=name, host=host_addr, user=user_val, port=port_val,
                    key_path=key_path, docker_mode=docker_mode)
    if auth_method == "password" and ssh_password.strip():
        save_credentials(slug, ssh_password=ssh_password.strip())

    return templates.TemplateResponse("partials/setup_ssh_section.html", {
        "request": request,
        "hosts": get_hosts(),
        "available_keys": get_available_ssh_keys(),
        "add_success": f"{name} added successfully.",
    })


@router.post("/setup/hosts/{slug}/remove", response_class=HTMLResponse)
async def setup_remove_host(request: Request, slug: str) -> HTMLResponse:
    delete_host(slug)
    delete_credentials(slug)
    return templates.TemplateResponse("partials/setup_ssh_section.html", {
        "request": request,
        "hosts": get_hosts(),
        "available_keys": get_available_ssh_keys(),
    })


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
        return HTMLResponse('<span class="text-amber-400 text-sm">Enter a URL and API token first.</span>')
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
        return HTMLResponse(f'<span class="text-red-400 text-sm">&#10007; {hint}</span>')


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
    return templates.TemplateResponse("partials/setup_portainer_section.html", {
        "request": request,
        "portainer_url": port_cfg.get("url", ""),
        "portainer_connected": portainer_connected,
        "portainer_saved": True,
    })


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
    return HTMLResponse('<p class="text-sm text-green-400">&#10003; DockerHub credentials saved.</p>')


@router.post("/setup/finish")
async def setup_finish(request: Request):
    return RedirectResponse("/login", status_code=303)
