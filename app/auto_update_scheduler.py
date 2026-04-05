import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .auto_update_log import append_log
from .config_manager import get_all_stack_auto_updates, get_hosts, get_ssh_config
from .notifications import notify
from .credentials import get_credentials
from .ssh_client import _needs_sudo, check_host_updates, reboot_host, run_host_update_buffered

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# Set by main.py on startup so stack jobs can reach the backends
_backends: list = []


def set_backends(backends: list) -> None:
    global _backends
    _backends = backends


# ---------------------------------------------------------------------------
# Job ID helpers
# ---------------------------------------------------------------------------

def _os_job_id(slug: str) -> str:
    return f"auto_os_{slug}"


def _stack_job_id(update_path: str) -> str:
    safe = update_path.replace("/", "_").replace(":", "_")
    return f"auto_stack_{safe}"


# ---------------------------------------------------------------------------
# Async job functions
# ---------------------------------------------------------------------------

async def _run_os_update(slug: str) -> None:
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    if not host:
        return

    au = host.get("auto_update", {})
    if not au.get("os_enabled"):
        return

    ssh_cfg = get_ssh_config()
    creds = get_credentials(slug)

    if _needs_sudo(host, ssh_cfg) and not creds.get("sudo_password"):
        append_log("os", slug, host["name"], "error",
                   ["Auto-update skipped: sudo password not stored. Save it via Admin → Hosts → Credentials."])
        notify(
            f"Auto-update skipped: {host['name']}",
            "sudo password not stored. Save it via Admin → Hosts → Credentials.",
        )
        return

    try:
        lines = await run_host_update_buffered(host, ssh_cfg, creds)
        append_log("os", slug, host["name"], "success", lines)

        if au.get("auto_reboot"):
            check = await check_host_updates(host, ssh_cfg, creds)
            if check.get("reboot_required"):
                await reboot_host(host, ssh_cfg, creds)
                append_log("os", slug, host["name"], "success",
                           ["Auto-reboot triggered — reboot-required flag was set after update."])
    except Exception as exc:
        logger.exception("Auto OS update failed for %s", slug)
        append_log("os", slug, host["name"], "error", [str(exc)])
        notify(f"Auto OS update failed: {host['name']}", str(exc))


async def _run_stack_update(update_path: str, stack_name: str) -> None:
    # update_path is "{backend_key}/{ref}", e.g. "portainer/3:1" or "ssh/myhost/mystack"
    parts = update_path.split("/", 1)
    if len(parts) != 2:
        append_log("docker", update_path, stack_name, "error",
                   [f"Invalid update_path format: {update_path!r}"])
        notify(
            f"Auto stack update failed: {stack_name}",
            f"Invalid update_path format: {update_path!r}",
        )
        return

    backend_key, ref = parts
    backend = next((b for b in _backends if b.BACKEND_KEY == backend_key), None)
    if backend is None:
        append_log("docker", update_path, stack_name, "error",
                   [f"Backend {backend_key!r} is not configured or not running."])
        notify(
            f"Auto stack update failed: {stack_name}",
            f"Backend {backend_key!r} is not configured or not running.",
        )
        return

    try:
        await backend.update_stack(ref)
        append_log("docker", update_path, stack_name, "success",
                   ["Stack redeployed — containers restarted with latest images."])
    except Exception as exc:
        logger.exception("Auto stack update failed for %s", update_path)
        append_log("docker", update_path, stack_name, "error", [str(exc)])
        notify(f"Auto stack update failed: {stack_name}", str(exc))


# ---------------------------------------------------------------------------
# Schedule management
# ---------------------------------------------------------------------------

def apply_host_schedule(slug: str) -> None:
    """Read host auto_update config and add/remove its scheduler job."""
    job_id = _os_job_id(slug)
    hosts = get_hosts()
    host = next((h for h in hosts if h["slug"] == slug), None)
    au = (host or {}).get("auto_update", {})

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if host and au.get("os_enabled") and au.get("os_schedule"):
        try:
            trigger = CronTrigger.from_crontab(au["os_schedule"])
            scheduler.add_job(_run_os_update, trigger, id=job_id, args=[slug], replace_existing=True)
            logger.info("Scheduled OS auto-update for %s: %s", slug, au["os_schedule"])
        except Exception as exc:
            logger.error("Invalid cron for host %s: %s", slug, exc)


def apply_stack_schedule(update_path: str) -> None:
    """Read stack auto_update config and add/remove its scheduler job."""
    job_id = _stack_job_id(update_path)
    all_stacks = get_all_stack_auto_updates()
    cfg = all_stacks.get(update_path, {})

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if cfg.get("enabled") and cfg.get("schedule"):
        stack_name = cfg.get("name", update_path)
        try:
            trigger = CronTrigger.from_crontab(cfg["schedule"])
            scheduler.add_job(_run_stack_update, trigger, id=job_id,
                              args=[update_path, stack_name], replace_existing=True)
            logger.info("Scheduled stack auto-update for %s: %s", update_path, cfg["schedule"])
        except Exception as exc:
            logger.error("Invalid cron for stack %s: %s", update_path, exc)


def apply_all_schedules() -> None:
    """Called once on startup to register all configured auto-update jobs."""
    for host in get_hosts():
        apply_host_schedule(host["slug"])
    for update_path in get_all_stack_auto_updates():
        apply_stack_schedule(update_path)
