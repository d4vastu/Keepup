import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .admin import router as admin_router
from .auth import admin_exists, get_session_secret
from .auth_router import router as auth_router
from .csrf import CSRFMiddleware
from .security_headers import ConditionalHTTPSRedirectMiddleware, SecurityHeadersMiddleware
from .log_buffer import setup_log_buffer
from .notifications import get_unread_count, get_notifications, mark_all_read
from .auto_update_scheduler import apply_all_schedules, scheduler
from .auto_updates_router import router as auto_updates_router
from .backend_loader import get_backends, get_dockerhub_creds, reload_backends
from .config_manager import get_hosts, get_ssh_config, get_pbs_config, get_proxmox_config
from .credentials import get_credentials, get_integration_credentials, save_sudo_password
from .ssh_client import (
    _needs_sudo,
    check_host_updates,
    reboot_host,
    run_host_update_buffered,
)
from .__version__ import APP_VERSION
from .self_identity import is_self_on_proxmox_node
from .ssl_manager import ssl_enabled
from .templates_env import make_templates

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub version check (cached 1 hour)
# ---------------------------------------------------------------------------

_version_cache: dict = {"ts": 0.0, "latest": None, "url": None}
_GITHUB_REPO = "d4vastu/Keepup"
_VERSION_CACHE_TTL = 3600


async def _fetch_latest_version() -> tuple[str | None, str | None]:
    """Return (latest_tag, release_url) or (None, None) on failure."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                return None, None
            data = resp.json()
            tag = data.get("tag_name", "").lstrip("v")
            url = data.get("html_url", "")
            return tag or None, url or None
    except Exception:
        return None, None


async def _get_latest_version() -> tuple[str | None, str | None]:
    """Return cached (latest_version, release_url); refresh if stale."""
    now = time.time()
    if now - _version_cache["ts"] > _VERSION_CACHE_TTL:
        tag, url = await _fetch_latest_version()
        _version_cache.update({"ts": now, "latest": tag, "url": url})
    return _version_cache["latest"], _version_cache["url"]


def _newer_version(latest: str | None) -> bool:
    """True if latest tag is strictly newer than APP_VERSION."""
    if not latest:
        return False
    try:

        def _parts(v: str) -> tuple:
            return tuple(int(x) for x in v.split("."))

        return _parts(latest) > _parts(APP_VERSION)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

_PUBLIC_PATHS = {
    "/",
    "/login",
    "/logout",
    "/setup",
    "/forgot-password",
    "/forgot-password/reset",
    # CSP violation reports are sent by the browser before auth state is
    # evaluated, so this endpoint must be accessible without a session.
    "/api/csp-report",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/setup/"):
            return await call_next(request)
        if not admin_exists():
            return RedirectResponse("/setup", status_code=302)
        if not request.session.get("authenticated"):
            return RedirectResponse(f"/login?next={path}", status_code=302)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

setup_log_buffer()
app = FastAPI(title="Keepup")

# ---------------------------------------------------------------------------
# Middleware stack — registration order is REVERSED from execution order.
# Starlette inserts each new middleware at position 0, so the last registered
# middleware wraps all the others (outermost = first in request chain).
#
# Resulting request chain (outermost → innermost):
#   ConditionalHTTPSRedirect → SecurityHeaders → Session → CSRF → Auth → routes
# ---------------------------------------------------------------------------

# Auth: protect every route that isn't on the public allow-list.
app.add_middleware(AuthMiddleware)

# CSRF: validate X-CSRF-Token on mutating HTMX requests; must run inside the
# Session context so it can read/write the session-stored token.
app.add_middleware(CSRFMiddleware)

# Session: load/store the signed session cookie.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="ud_session",
    max_age=30 * 24 * 3600,  # 30 days max; login sets shorter if no remember_me
    # Enable the Secure flag whenever TLS is active so the cookie is not sent
    # over plain HTTP connections.
    https_only=ssl_enabled(),
    # SameSite=strict prevents the browser from sending the session cookie on
    # any cross-site request, giving strong CSRF protection for non-HTMX forms.
    same_site="strict",
)

# Security headers: inject X-Content-Type-Options, X-Frame-Options, etc.
# Runs outside the session layer so headers are added to every response
# including redirects and error pages.
_tls_active = ssl_enabled()
app.add_middleware(SecurityHeadersMiddleware, tls_active=_tls_active)

# HTTPS redirect: must be the outermost middleware so that HTTP requests are
# redirected before any session processing or auth checks occur.
app.add_middleware(ConditionalHTTPSRedirectMiddleware, tls_active=_tls_active)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(auto_updates_router)
app.mount(
    "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
)
templates = make_templates()

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_VERSION_FILE = _DATA_DIR / ".app_version"


def _check_version_notification() -> None:
    """On first startup after a new version, create a notification with a release notes link."""
    try:
        stored = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else ""
        if stored != APP_VERSION:
            if stored:  # only notify on upgrade, not on fresh install
                from .notifications import notify

                notify(
                    f"Updated to v{APP_VERSION}",
                    f"Keepup was upgraded from v{stored} to v{APP_VERSION}.",
                    level="info",
                    url=f"https://github.com/d4vastu/Keepup/releases/tag/v{APP_VERSION}",
                )
            _VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _VERSION_FILE.write_text(APP_VERSION)
    except Exception:
        pass


@app.on_event("startup")
async def _startup() -> None:
    _check_version_notification()
    await reload_backends()
    apply_all_schedules()
    scheduler.start()


# ---------------------------------------------------------------------------
# CSP violation report endpoint
# ---------------------------------------------------------------------------


@app.post("/api/csp-report", status_code=204)
async def csp_report(request: Request) -> None:
    """Receive Content-Security-Policy violation reports from browsers.

    Browsers POST a JSON body when a resource is blocked (or would be blocked
    in Report-Only mode). We log the report at WARNING level so violations are
    visible in the Keepup logs page. The endpoint is public (no auth required)
    because the browser sends reports before authentication state is evaluated.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    report = body.get("csp-report") or body
    log.warning("CSP violation: %s", report)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_host(slug: str) -> dict:
    for h in get_hosts():
        if h["slug"] == slug:
            return h
    raise KeyError(f"Host {slug!r} not in config")


def _group_hosts(hosts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (proxmox_groups, standalone_hosts).

    Each group: {name, node_host, lxcs, vms} where node_host may be None
    if the node itself isn't monitored.
    """
    nodes: dict[str, dict] = {}
    standalone: list[dict] = []
    for h in hosts:
        node = h.get("proxmox_node")
        if not node:
            standalone.append(h)
            continue
        if node not in nodes:
            nodes[node] = {"name": node, "node_host": None, "lxcs": [], "vms": []}
        vmid = h.get("proxmox_vmid")
        if vmid is None:
            nodes[node]["node_host"] = h
        else:
            ptype = h.get("proxmox_type") or "lxc"
            if ptype == "vm":
                nodes[node]["vms"].append(h)
            else:
                nodes[node]["lxcs"].append(h)
    return list(nodes.values()), standalone


_jobs: dict[str, dict] = {}


def _classify_log_line(line: str) -> str:
    lowered = line.lower()
    if any(k in lowered for k in ("error", "fail", "e:")):
        return "log-line-red"
    if any(k in lowered for k in ("warn", "reboot")):
        return "log-line-yellow"
    if any(
        line.startswith(p) for p in ("Get:", "Unpacking", "Setting up", "Processing")
    ):
        return "log-line-dim"
    if any(k in lowered for k in ("upgraded", "installed", "done")):
        return "log-line-ok"
    return "log-line-white"


def _format_log_lines(lines: list[str]) -> str:
    import html as _html

    parts = []
    for line in lines:
        cls = _classify_log_line(line)
        parts.append(f'<div class="{cls}">{_html.escape(line)}</div>')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------


async def _job_run_host_update(job_id: str, host: dict, creds: dict) -> None:
    name = host.get("name", host.get("host", "unknown"))
    try:
        lines = await run_host_update_buffered(host, get_ssh_config(), creds)
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
        log.info("OS upgrade complete on %s", name)
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        log.error("OS upgrade failed on %s: %s", name, exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_proxmox_node_upgrade(job_id: str, slug: str) -> None:
    try:
        host = _get_host(slug)
        ssh_cfg = get_ssh_config()
        creds = get_credentials(slug)
        lines = await run_host_update_buffered(host, ssh_cfg, creds)
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
        log.info("Proxmox node upgrade complete: %s", slug)
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        log.error("Proxmox node upgrade failed on %s: %s", slug, exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_lxc_upgrade(
    job_id: str, node: str, vmid: int, ssh_host: str
) -> None:
    try:
        client = await _proxmox_client_from_config()
        px_creds = get_integration_credentials("proxmox")
        ssh_creds = {
            "user": px_creds.get("ssh_user", "root"),
            "port": px_creds.get("ssh_port", 22),
            "key_path": px_creds.get("ssh_key_path", ""),
            "ssh_password": px_creds.get("ssh_password", ""),
        }
        lines = await client.upgrade_lxc(node, vmid, ssh_host, get_ssh_config(), ssh_creds)
        _jobs[job_id]["lines"] = lines
        _jobs[job_id]["status"] = "done"
        log.info("LXC upgrade complete: %s/%s", node, vmid)
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        log.error("LXC upgrade failed on %s/%s: %s", node, vmid, exc)
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


async def _job_run_proxmox_node_restart(
    job_id: str, slug: str, proxmox_node: str
) -> None:
    try:
        client = await _proxmox_client_from_config()

        _jobs[job_id]["lines"].append(f"Issuing reboot to node {proxmox_node}…")
        await client.reboot_node(proxmox_node)

        _jobs[job_id]["lines"].append("Waiting for node to return…")
        up = await client.wait_for_node(proxmox_node)
        if not up:
            _jobs[job_id]["lines"].append("Node did not come back within 10 minutes.")
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = "Node did not come back within 10 minutes"
        else:
            kernel = await client.get_node_kernel(proxmox_node)
            _jobs[job_id]["lines"].append(f"Node up — kernel now {kernel}")
            _jobs[job_id]["status"] = "done"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        log.error("Proxmox node restart failed on %s: %s", slug, exc)
    finally:
        _jobs[job_id]["done"] = True


async def _job_run_stack_update(job_id: str, backend_key: str, ref: str) -> None:
    try:
        backend = next(
            (b for b in get_backends() if b.BACKEND_KEY == backend_key), None
        )
        if backend is None:
            raise ValueError(f"Backend {backend_key!r} not available")
        await backend.update_stack(ref)
        _jobs[job_id]["lines"] = [
            "Stack updated — containers restarted with new images."
        ]
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
async def home(request: Request) -> HTMLResponse:
    if request.session.get("authenticated"):
        return RedirectResponse("/home", status_code=302)
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/home", response_class=HTMLResponse)
async def main_home(request: Request) -> HTMLResponse:
    hosts = get_hosts()
    backends = get_backends()
    docker_configured = any(b.BACKEND_KEY == "portainer" for b in backends) or any(
        h.get("docker_mode") for h in hosts
    )
    pbs_creds = get_integration_credentials("proxmox_backup")
    pbs_cfg = get_pbs_config()
    pbs_configured = bool(
        pbs_cfg.get("url") and (pbs_creds.get("secret") or pbs_creds.get("api_token"))
    )
    latest_tag, latest_url = await _get_latest_version()
    show_update = _newer_version(latest_tag)
    host_groups, standalone_hosts = _group_hosts(hosts)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "hosts": hosts,
            "host_groups": host_groups,
            "standalone_hosts": standalone_hosts,
            "docker_configured": docker_configured,
            "pbs_configured": pbs_configured,
            "app_version": APP_VERSION,
            "latest_version": latest_tag if show_update else None,
            "latest_release_url": latest_url if show_update else None,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_redirect(request: Request) -> HTMLResponse:
    return RedirectResponse("/home", status_code=301)


@app.get("/api/integration/pbs/status", response_class=HTMLResponse)
async def pbs_status(request: Request) -> HTMLResponse:
    """Return a small status card for PBS — version and connectivity."""
    import httpx
    import logging

    log = logging.getLogger(__name__)
    cfg = get_pbs_config()
    creds = get_integration_credentials("proxmox_backup")
    url = cfg.get("url", "")
    verify_ssl = cfg.get("verify_ssl", False)
    token_id = creds.get("token_id", "")
    secret = creds.get("secret", "")
    # Legacy fallback
    if not token_id:
        api_user = creds.get("api_user", "")
        api_token = creds.get("api_token", "")
        auth = f"PBSAPIToken={api_user}!{api_token}" if api_user else f"PBSAPIToken={api_token}"
    else:
        auth = f"PBSAPIToken={token_id}:{secret}"

    if not url or not (token_id or creds.get("api_token")):
        return HTMLResponse("")

    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=8) as c:
            resp = await c.get(f"{url}/api2/json/version", headers={"Authorization": auth})
            resp.raise_for_status()
            ver = resp.json().get("data", {}).get("version", "unknown")
        return templates.TemplateResponse(
            "partials/integration_status_card.html",
            {"request": request, "name": "Proxmox Backup Server", "version": ver, "ok": True},
        )
    except Exception as exc:
        log.warning("PBS status check failed: %s", exc)
        return templates.TemplateResponse(
            "partials/integration_status_card.html",
            {"request": request, "name": "Proxmox Backup Server", "version": None, "ok": False},
        )


# ---------------------------------------------------------------------------
# Routes — OS updates
# ---------------------------------------------------------------------------


async def _proxmox_client_from_config():
    """Build a ProxmoxClient from the stored integration config."""
    from .proxmox_client import ProxmoxClient

    cfg = get_proxmox_config()
    creds = get_integration_credentials("proxmox")
    url = cfg.get("url", "")
    token_id = creds.get("token_id", "")
    secret = creds.get("secret", "")
    if not token_id:
        api_user = creds.get("api_user", "")
        api_token = creds.get("api_token", "")
        token = f"{api_user}!{api_token}" if api_user else api_token
    else:
        token = f"{token_id}={secret}"
    verify_ssl = cfg.get("verify_ssl", False)
    if not url or not token:
        raise RuntimeError("Proxmox integration not configured.")
    return ProxmoxClient(url=url, api_token=token, verify_ssl=verify_ssl)


@app.get("/api/host/{slug}/check", response_class=HTMLResponse)
async def host_check(request: Request, slug: str) -> HTMLResponse:
    try:
        host = _get_host(slug)
        host_name = host.get("name", slug)
        proxmox_node = host.get("proxmox_node")
        proxmox_vmid = host.get("proxmox_vmid")
        if proxmox_node and proxmox_vmid is not None:
            # LXC container — use pct exec via SSH to the Proxmox host
            log.info(
                "Checking %s (%s) via pct exec (%s/%s)",
                host_name, slug, proxmox_node, proxmox_vmid,
            )
            from .credentials import get_integration_credentials as _get_int_creds
            px_creds = _get_int_creds("proxmox")
            ssh_user = px_creds.get("ssh_user", "root")
            ssh_key = px_creds.get("ssh_key", "")
            ssh_password = px_creds.get("ssh_password", "")
            px_cfg = get_proxmox_config()
            proxmox_url = px_cfg.get("url", "")
            import urllib.parse
            px_host = urllib.parse.urlparse(proxmox_url).hostname or host["host"]
            ssh_key_path = f"/app/keys/{ssh_key}" if ssh_key else None
            ssh_creds: dict = {"user": ssh_user}
            if ssh_key_path:
                ssh_creds["key_path"] = ssh_key_path
            elif ssh_password:
                ssh_creds["ssh_password"] = ssh_password
            client = await _proxmox_client_from_config()
            packages = await client.get_lxc_updates(
                proxmox_node, proxmox_vmid, px_host, get_ssh_config(), ssh_creds
            )
            log.info(
                "Check complete: %s (%s) — %d update(s) via pct exec",
                host_name, slug, len(packages),
            )
            return templates.TemplateResponse(
                "partials/host_status.html",
                {
                    "request": request,
                    "slug": slug,
                    "packages": packages,
                    "reboot_required": False,
                    "is_proxmox_node": False,
                    "package_manager": f"apt · pct exec ({proxmox_node}/{proxmox_vmid})",
                    "proxmox_node": proxmox_node,
                    "proxmox_url": proxmox_url,
                },
            )
        if proxmox_node:
            log.info(
                "Checking %s (%s) via Proxmox API (node %s)",
                host_name, slug, proxmox_node,
            )
            import asyncio as _asyncio
            client = await _proxmox_client_from_config()
            packages, reboot_required = await _asyncio.gather(
                client.get_node_updates(proxmox_node),
                client.get_node_reboot_required(proxmox_node),
            )
            proxmox_url = get_proxmox_config().get("url", "")
            log.info(
                "Check complete: %s (%s) — %d update(s), reboot_required=%s via Proxmox API",
                host_name, slug, len(packages), reboot_required,
            )
            return templates.TemplateResponse(
                "partials/host_status.html",
                {
                    "request": request,
                    "slug": slug,
                    "packages": packages,
                    "reboot_required": reboot_required,
                    "is_proxmox_node": True,
                    "package_manager": f"apt · Proxmox API ({proxmox_node})",
                    "proxmox_node": proxmox_node,
                    "proxmox_url": proxmox_url,
                },
            )
        log.info("Checking %s (%s) via SSH", host_name, slug)
        creds = get_credentials(slug)
        result = await check_host_updates(host, get_ssh_config(), creds)
        log.info(
            "Check complete: %s (%s) — %d update(s) via SSH",
            host_name, slug, len(result["packages"]),
        )
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
        log.exception("host_check failed for %s: %s", slug, exc)
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
        host_name = host.get("name", slug)
        log.info("Running OS upgrade on %s (%s)", host_name, slug)

        proxmox_node = host.get("proxmox_node")
        proxmox_vmid = host.get("proxmox_vmid")
        if proxmox_node and proxmox_vmid is None:
            job_id = uuid.uuid4().hex[:8]
            _jobs[job_id] = {
                "done": False,
                "status": "running",
                "error": None,
                "lines": [],
                "type": "os_upgrade",
                "label": host_name,
                "sub": proxmox_node,
            }
            background_tasks.add_task(_job_run_proxmox_node_upgrade, job_id, slug)
            return templates.TemplateResponse(
                "partials/job_poll.html",
                {"request": request, "job_id": job_id, "job": _jobs[job_id]},
            )

        if proxmox_node and proxmox_vmid is not None:
            proxmox_url = get_proxmox_config().get("url", "")
            import urllib.parse as _up
            ssh_host = _up.urlparse(proxmox_url).hostname or host.get("host", "")
            job_id = uuid.uuid4().hex[:8]
            _jobs[job_id] = {
                "done": False,
                "status": "running",
                "error": None,
                "lines": [],
                "type": "os_upgrade",
                "label": host_name,
                "sub": f"{proxmox_node}/{proxmox_vmid}",
            }
            background_tasks.add_task(
                _job_run_lxc_upgrade, job_id, proxmox_node, proxmox_vmid, ssh_host
            )
            return templates.TemplateResponse(
                "partials/job_poll.html",
                {"request": request, "job_id": job_id, "job": _jobs[job_id]},
            )

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
        _jobs[job_id] = {
            "done": False,
            "status": "running",
            "error": None,
            "lines": [],
            "type": "os_upgrade",
            "label": host["name"],
            "sub": host.get("host", ""),
        }
        background_tasks.add_task(_job_run_host_update, job_id, host, creds)
        return templates.TemplateResponse(
            "partials/job_poll.html",
            {"request": request, "job_id": job_id, "job": _jobs[job_id]},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.get("/api/host/{slug}/reboot-preview", response_class=HTMLResponse)
async def host_reboot_preview(request: Request, slug: str) -> HTMLResponse:
    try:
        host = _get_host(slug)
        proxmox_node = host.get("proxmox_node")
        proxmox_vmid = host.get("proxmox_vmid")

        if proxmox_node and proxmox_vmid is None:
            self_on_node = is_self_on_proxmox_node(proxmox_node)
            client = await _proxmox_client_from_config()
            guests = await client.get_running_guests(proxmox_node)
            return templates.TemplateResponse(
                "partials/proxmox_reboot_preview.html",
                {
                    "request": request,
                    "slug": slug,
                    "proxmox_node": proxmox_node,
                    "guests": guests,
                    "self_on_node": self_on_node,
                },
            )

        # Non-Proxmox or LXC: trigger the simple reboot directly
        return templates.TemplateResponse(
            "partials/proxmox_reboot_preview.html",
            {
                "request": request,
                "slug": slug,
                "proxmox_node": None,
                "guests": [],
                "self_on_node": False,
            },
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
    confirmed: str = Form(""),
    force_stop: str = Form(""),
) -> HTMLResponse:
    try:
        host = _get_host(slug)
        proxmox_node = host.get("proxmox_node")
        proxmox_vmid = host.get("proxmox_vmid")

        if proxmox_node and proxmox_vmid is None:
            host_name = host.get("name", slug)
            job_id = uuid.uuid4().hex[:8]
            _jobs[job_id] = {
                "done": False,
                "status": "running",
                "error": None,
                "lines": [],
                "type": "os_restart",
                "label": host_name,
                "sub": proxmox_node,
                "slug": slug,
            }
            background_tasks.add_task(
                _job_run_proxmox_node_restart,
                job_id, slug, proxmox_node,
            )
            return templates.TemplateResponse(
                "partials/job_poll.html",
                {"request": request, "job_id": job_id, "job": _jobs[job_id]},
            )

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
        _jobs[job_id] = {
            "done": False,
            "status": "running",
            "error": None,
            "lines": [],
            "type": "os_restart",
            "label": host["name"],
            "sub": host.get("host", ""),
        }
        background_tasks.add_task(_job_run_host_restart, job_id, host, creds)
        return templates.TemplateResponse(
            "partials/job_poll.html",
            {"request": request, "job_id": job_id, "job": _jobs[job_id]},
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
    backends = get_backends()
    active = [
        b
        for b in backends
        if b.BACKEND_KEY != "ssh" or any(h.get("docker_mode") for h in hosts)
    ]
    if not active:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "No container backends configured."},
        )
    try:
        results = await asyncio.gather(
            *[b.get_stacks_with_update_status(get_dockerhub_creds()) for b in active],
            return_exceptions=True,
        )
        stacks = []
        for r in results:
            if isinstance(r, list):
                stacks.extend(r)
        updates_available = sum(
            1 for s in stacks if s.get("update_status") in ("update_available", "mixed")
        )
        log.info(
            "Container check: found %d stack(s) with updates (of %d total)",
            updates_available, len(stacks),
        )
        # Check for new image updates and fire notifications (deduplicated)
        try:
            from .update_notifier import check_and_notify

            check_and_notify(stacks)
        except Exception:
            pass
        return templates.TemplateResponse(
            "partials/docker_status.html",
            {"request": request, "stacks": stacks},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc)},
        )


@app.post(
    "/api/docker/stack/{backend_key}/{ref:path}/update", response_class=HTMLResponse
)
async def stack_update(
    request: Request,
    backend_key: str,
    ref: str,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    backend = next((b for b in get_backends() if b.BACKEND_KEY == backend_key), None)
    if not backend:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"Backend {backend_key!r} not configured."},
        )
    job_id = uuid.uuid4().hex[:8]
    stack_name = ref.rsplit("/", 1)[-1]
    _jobs[job_id] = {
        "done": False,
        "status": "running",
        "error": None,
        "lines": [],
        "type": "container_redeploy",
        "label": stack_name,
        "sub": backend_key,
    }
    background_tasks.add_task(_job_run_stack_update, job_id, backend_key, ref)
    return templates.TemplateResponse(
        "partials/job_poll.html",
        {"request": request, "job_id": job_id, "job": _jobs[job_id]},
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


@app.get("/api/jobs/{job_id}/modal", response_class=HTMLResponse)
async def job_modal(request: Request, job_id: str) -> HTMLResponse:
    job = _jobs.get(job_id)
    if not job:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "partials/upgrade_modal.html",
        {
            "request": request,
            "job_id": job_id,
            "job": job,
            "log_html": _format_log_lines(job.get("lines", [])),
        },
    )


@app.get("/api/jobs/{job_id}/modal-body", response_class=HTMLResponse)
async def job_modal_body(request: Request, job_id: str) -> HTMLResponse:
    job = _jobs.get(job_id)
    if not job:
        return HTMLResponse("")
    return HTMLResponse(_format_log_lines(job.get("lines", [])))


# ---------------------------------------------------------------------------
# Routes — Notifications
# ---------------------------------------------------------------------------


@app.get("/api/notifications/badge", response_class=HTMLResponse)
async def notifications_badge(request: Request) -> HTMLResponse:
    count = get_unread_count()
    if count == 0:
        return HTMLResponse(
            '<span id="notif-badge" hx-get="/api/notifications/badge" '
            'hx-trigger="every 60s" hx-swap="outerHTML"></span>'
        )
    label = str(count) if count < 10 else "9+"
    return HTMLResponse(
        f'<span id="notif-badge" hx-get="/api/notifications/badge" '
        f'hx-trigger="every 60s" hx-swap="outerHTML" '
        f'class="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-red-500 '
        f'text-xs flex items-center justify-center text-white font-bold pointer-events-none">'
        f"{label}</span>"
    )


@app.get("/api/notifications/panel", response_class=HTMLResponse)
async def notifications_panel(request: Request) -> HTMLResponse:
    entries = get_notifications(20)
    return templates.TemplateResponse(
        "partials/notification_panel.html",
        {"request": request, "entries": entries},
    )


@app.post("/api/notifications/read", response_class=HTMLResponse)
async def notifications_read(request: Request) -> HTMLResponse:
    mark_all_read()
    return HTMLResponse(
        '<span id="notif-badge" hx-get="/api/notifications/badge" '
        'hx-trigger="every 60s" hx-swap="outerHTML"></span>'
    )


@app.post("/api/reload-connections", response_class=HTMLResponse)
async def reload_connections(request: Request) -> HTMLResponse:
    """Reinitialise backends after connection settings change."""
    await reload_backends()
    return HTMLResponse("")
