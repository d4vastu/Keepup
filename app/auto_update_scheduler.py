import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .activity_log import exc_text, record_run
from .config_manager import get_all_stack_auto_updates, get_hosts
from .notifications import notify
from .credentials import get_credentials
from .host_ops import reboot_host_typed, reboot_required_typed, run_os_update
from .ssh_client import _needs_sudo

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

    creds = get_credentials(slug)

    if _needs_sudo(host) and not creds.get("sudo_password"):
        # Nothing ran, so this is "skipped" rather than a failure — it keeps
        # the error filter on the activity page meaning "something broke".
        record_run(
            kind="os_upgrade",
            target=slug,
            target_name=host["name"],
            trigger="scheduled",
            status="skipped",
            output=[
                "Auto-update skipped: sudo password not stored."
                " Save it via Admin → Hosts → Credentials."
            ],
            error="sudo password not stored",
        )
        notify(
            f"Auto-update skipped: {host['name']}",
            "sudo password not stored. Save it via Admin → Hosts → Credentials.",
        )
        return

    started = datetime.now(timezone.utc).isoformat()
    try:
        lines = await run_os_update(host, creds)
        record_run(
            kind="os_upgrade",
            target=slug,
            target_name=host["name"],
            trigger="scheduled",
            status="success",
            output=lines,
            started_at=started,
        )

        if au.get("auto_reboot"):
            if await reboot_required_typed(host, creds):
                reboot_started = datetime.now(timezone.utc).isoformat()
                reboot_lines = await reboot_host_typed(host, creds)
                # A reboot is its own run, not a second os_upgrade entry —
                # which is all it ever was under the old log.
                record_run(
                    kind="reboot",
                    target=slug,
                    target_name=host["name"],
                    trigger="scheduled",
                    status="success",
                    output=list(reboot_lines or [])
                    + [
                        "Auto-reboot triggered — reboot-required flag was set after update."
                    ],
                    started_at=reboot_started,
                )
    except Exception as exc:
        logger.exception("Auto OS update failed for %s", slug)
        record_run(
            kind="os_upgrade",
            target=slug,
            target_name=host["name"],
            trigger="scheduled",
            status="error",
            output=[exc_text(exc)],
            started_at=started,
            error=exc_text(exc),
        )
        notify(f"Auto OS update failed: {host['name']}", exc_text(exc))


async def _run_stack_update(update_path: str, stack_name: str) -> None:
    # update_path is "{backend_key}/{ref}", e.g. "portainer/3:1" or "ssh/myhost/mystack"
    parts = update_path.split("/", 1)
    if len(parts) != 2:
        message = f"Invalid update_path format: {update_path!r}"
        record_run(
            kind="container_redeploy",
            target=update_path,
            target_name=stack_name,
            trigger="scheduled",
            status="error",
            output=[message],
            error=message,
        )
        notify(f"Auto stack update failed: {stack_name}", message)
        return

    backend_key, ref = parts
    backend = next((b for b in _backends if b.BACKEND_KEY == backend_key), None)
    if backend is None:
        message = f"Backend {backend_key!r} is not configured or not running."
        record_run(
            kind="container_redeploy",
            target=update_path,
            target_name=stack_name,
            trigger="scheduled",
            status="error",
            output=[message],
            error=message,
        )
        notify(f"Auto stack update failed: {stack_name}", message)
        return

    started = datetime.now(timezone.utc).isoformat()
    try:
        lines = await backend.update_stack(ref)
        record_run(
            kind="container_redeploy",
            target=update_path,
            target_name=stack_name,
            trigger="scheduled",
            status="success",
            output=list(lines or [])
            or ["Stack redeployed — containers restarted with latest images."],
            started_at=started,
        )
    except Exception as exc:
        logger.exception("Auto stack update failed for %s", update_path)
        record_run(
            kind="container_redeploy",
            target=update_path,
            target_name=stack_name,
            trigger="scheduled",
            status="error",
            # A StackUpdateError carries the output captured before it failed.
            output=list(getattr(exc, "lines", [])) or [exc_text(exc)],
            started_at=started,
            error=exc_text(exc),
        )
        notify(f"Auto stack update failed: {stack_name}", exc_text(exc))


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
            scheduler.add_job(
                _run_os_update, trigger, id=job_id, args=[slug], replace_existing=True
            )
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
            scheduler.add_job(
                _run_stack_update,
                trigger,
                id=job_id,
                args=[update_path, stack_name],
                replace_existing=True,
            )
            logger.info(
                "Scheduled stack auto-update for %s: %s", update_path, cfg["schedule"]
            )
        except Exception as exc:
            logger.error("Invalid cron for stack %s: %s", update_path, exc)


def apply_all_schedules() -> None:
    """Called once on startup to register all configured auto-update jobs."""
    for host in get_hosts():
        apply_host_schedule(host["slug"])
    for update_path in get_all_stack_auto_updates():
        apply_stack_schedule(update_path)
