"""Update detection, shared by the dashboard routes and the scheduled job.

Detection used to live inline in two request handlers (`docker_check` and
`host_check`), which is why nothing could check for updates without a browser
open. It lives here once: the routes render what these functions return, and the
scheduled job calls the same functions with no HTTP request involved. Keeping
one copy is also what stops a check and an upgrade from resolving different
Proxmox servers for the same guest (OP#210).

Nothing in this module renders HTML or touches a Request.
"""

import asyncio
import logging

from .activity_log import exc_text
from .backend_loader import get_backends, get_dockerhub_creds
from .config_manager import get_hosts, get_proxmox_config
from .credentials import get_credentials
from .host_ops import reboot_required_typed
from .last_check import record_container_check, record_host_check
from .notifications import notify
from .proxmox_client import client_from_config
from .ssh_client import check_host_updates
from .update_notifier import check_and_notify, should_notify_host

logger = logging.getLogger(__name__)

# Enough to keep a scan brisk, few enough that the job never opens an SSH
# connection to every host at once.
MAX_CONCURRENT_HOST_CHECKS = 4

# A whole-host budget above `ssh_client`'s own per-operation timeouts, so a host
# wedged somewhere those do not cover cannot hold the scan open indefinitely.
# Generous on purpose: a slow but working `apt-get update` must finish, or the
# job reports a failure that is not one (OP#228).
HOST_SCAN_TIMEOUT = 300

_BACKEND_LABELS = {"portainer": "Portainer", "ssh": "SSH"}


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


async def scan_host(host: dict, creds: dict) -> dict:
    """Check one host for OS package updates.

    Returns the fields `partials/host_status.html` renders, so the route is a
    renderer and the job is a caller of the very same code.
    """
    slug = host.get("slug", "")
    name = host.get("name", slug)
    proxmox_node = host.get("proxmox_node")
    proxmox_vmid = host.get("proxmox_vmid")

    if proxmox_node and proxmox_vmid is not None:
        # Same resolution the upgrade path uses, so a check and an upgrade of one
        # LXC can never target different servers (OP#210).
        from .host_ops import _lxc_ssh_context, server_context

        px_host, ssh_creds = _lxc_ssh_context(host)
        proxmox_url = server_context(host)[0].get("url", "")
        client = client_from_config(host.get("proxmox_server"))
        packages = await client.get_lxc_updates(
            proxmox_node, proxmox_vmid, px_host, ssh_creds
        )
        logger.info(
            "Check complete: %s (%s) — %d update(s) via pct exec", name, slug, len(packages)
        )
        return {
            "packages": packages,
            "reboot_required": False,
            "is_proxmox_node": False,
            "package_manager": f"apt · pct exec ({proxmox_node}/{proxmox_vmid})",
            "proxmox_node": proxmox_node,
            "proxmox_url": proxmox_url,
        }

    if proxmox_node:
        client = client_from_config(host.get("proxmox_server"))
        packages, reboot_required = await asyncio.gather(
            client.get_node_updates(proxmox_node),
            reboot_required_typed(host, {}),
        )
        logger.info(
            "Check complete: %s (%s) — %d update(s), reboot_required=%s via Proxmox API",
            name, slug, len(packages), reboot_required,
        )
        return {
            "packages": packages,
            "reboot_required": reboot_required,
            "is_proxmox_node": True,
            "package_manager": f"apt · Proxmox API ({proxmox_node})",
            "proxmox_node": proxmox_node,
            "proxmox_url": get_proxmox_config().get("url", ""),
        }

    result = await check_host_updates(host, creds)
    logger.info(
        "Check complete: %s (%s) — %d update(s) via SSH", name, slug, len(result["packages"])
    )
    return {
        "packages": result["packages"],
        "reboot_required": result["reboot_required"],
        "is_proxmox_node": False,
        "package_manager": result.get("package_manager", ""),
        "proxmox_node": None,
        "proxmox_url": "",
    }


async def scan_hosts(hosts: list[dict]) -> list[tuple[dict, dict | Exception]]:
    """Check every host, bounded, returning each host beside its result.

    One unreachable host must not cost the others their check, so failures come
    back as exceptions rather than aborting the gather. They are logged through
    `exc_text` because a bare `TimeoutError()` stringifies to nothing.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENT_HOST_CHECKS)

    async def one(host: dict):
        async with sem:
            try:
                return await asyncio.wait_for(
                    scan_host(host, get_credentials(host.get("slug", ""))),
                    timeout=HOST_SCAN_TIMEOUT,
                )
            except Exception as e:
                logger.warning(
                    "Update check failed for %s: %s",
                    host.get("name", host.get("slug", "?")),
                    exc_text(e),
                )
                return e

    results = await asyncio.gather(*[one(h) for h in hosts])
    return list(zip(hosts, results))


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


async def scan_containers() -> dict:
    """Check every configured container backend for image updates."""
    hosts = get_hosts()
    active = [
        b
        for b in get_backends()
        if b.BACKEND_KEY != "ssh" or any(h.get("docker_mode") for h in hosts)
    ]
    if not active:
        return {"stacks": [], "failed_backends": [], "backends_configured": False}

    results = await asyncio.gather(
        *[b.get_stacks_with_update_status(get_dockerhub_creds()) for b in active],
        return_exceptions=True,
    )

    stacks: list = []
    failed_backends: list[str] = []
    for backend, r in zip(active, results):
        if isinstance(r, Exception):
            logger.warning(
                "Container backend '%s' failed during check: %s",
                backend.BACKEND_KEY, exc_text(r),
            )
            failed_backends.append(
                _BACKEND_LABELS.get(backend.BACKEND_KEY, backend.BACKEND_KEY.title())
            )
        elif isinstance(r, list):
            stacks.extend(r)

    try:
        check_and_notify(stacks)
    except Exception as e:
        logger.warning("Container update notification failed: %s", exc_text(e))

    return {
        "stacks": stacks,
        "failed_backends": failed_backends,
        "backends_configured": True,
    }


# ---------------------------------------------------------------------------
# The job body
# ---------------------------------------------------------------------------


async def run_scan() -> None:
    """One full detection pass over hosts and containers.

    Called by the scheduler, and by nothing that has a Request in scope.
    """
    hosts = get_hosts()
    for host, result in await scan_hosts(hosts):
        if isinstance(result, Exception):
            # No timestamp for a check that never completed — recording one
            # would claim a freshness the dashboard does not have.
            continue
        slug = host.get("slug", "")
        record_host_check(slug)
        _notify_host(host, result)

    try:
        await scan_containers()
        record_container_check()
    except Exception as e:
        logger.warning("Container scan failed: %s", exc_text(e))


def _notify_host(host: dict, result: dict) -> None:
    """Fire one notification per host that has gone from clean to pending.

    Deduplicated per host and cleared when the host is up to date again, so a
    six-hourly job does not re-announce the same packages forever.
    """
    packages = result.get("packages") or []
    reboot = bool(result.get("reboot_required"))
    slug = host.get("slug", "")
    name = host.get("name", slug)

    if not should_notify_host(slug, bool(packages) or reboot):
        return

    if packages:
        count = len(packages)
        message = f"{count} package update{'s' if count != 1 else ''} available"
        if reboot:
            message += " · reboot required"
    else:
        message = "Reboot required"

    notify(f"{name}: updates available", message, level="warning", url="/home")
