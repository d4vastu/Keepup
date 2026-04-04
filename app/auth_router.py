import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    admin_exists,
    create_admin,
    get_totp_uri,
    mfa_enrolled,
    new_totp_secret,
    reset_password_with_backup_key,
    verify_backup_key,
    verify_password,
    verify_totp,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

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
    password: str = Form(""),
    password_confirm: str = Form(""),
    totp_code: str = Form(""),
    enable_mfa: str = Form(""),
) -> HTMLResponse:
    if admin_exists():
        return RedirectResponse("/", status_code=302)

    errors: list[str] = []

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
        })

    backup_key = create_admin(password=password, totp_secret=totp_secret)
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
    return RedirectResponse("/login", status_code=303)


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
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(""),
    totp_code: str = Form(""),
    remember_me: str = Form(""),
) -> HTMLResponse:
    ip = _client_ip(request)
    allowed, remaining = _check_rate_limit(ip)

    if not allowed:
        mins = remaining // 60 + 1
        return templates.TemplateResponse("login.html", {
            "request": request,
            "needs_mfa": mfa_enrolled(),
            "error": f"Too many failed attempts. Try again in {mins} minute{'s' if mins != 1 else ''}.",
        })

    ok = verify_password(password)
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
            error = "Incorrect password or authenticator code."
        return templates.TemplateResponse("login.html", {
            "request": request,
            "needs_mfa": mfa_enrolled(),
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
