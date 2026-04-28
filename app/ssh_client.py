import asyncio
import logging

import asyncssh

from .package_managers import DETECT_CMD, get_package_manager

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15
_COMMAND_TIMEOUT = 600
_CHECK_TIMEOUT = 60


def _needs_sudo(host: dict) -> bool:
    return host.get("user", "") != "root"


async def _connect(
    host: dict, creds: dict | None = None
) -> asyncssh.SSHClientConnection:
    user = (host.get("user") or "").strip()
    if not user:
        raise ValueError(
            f"Host {host.get('name', host['host'])!r} has no SSH user configured. "
            "Set the SSH user in Admin › Hosts."
        )
    creds = creds or {}
    kwargs: dict = {
        "host": host["host"],
        "port": host.get("port", 22),
        "username": user,
        "known_hosts": None,
        "connect_timeout": _CONNECT_TIMEOUT,
    }
    if creds.get("ssh_password"):
        kwargs["password"] = creds["ssh_password"]
        kwargs["preferred_auth"] = "password"
    elif creds.get("ssh_key"):
        key = asyncssh.import_private_key(creds["ssh_key"])
        kwargs["client_keys"] = [key]
    else:
        key_path = host.get("key")
        if key_path:
            kwargs["client_keys"] = [key_path]
    return await asyncssh.connect(**kwargs)


async def _run(
    conn: asyncssh.SSHClientConnection,
    cmd: str,
    sudo_password: str | None,
    needs_sudo: bool,
    timeout: int | None = None,
) -> asyncssh.SSHCompletedProcess:
    """Run a command, wrapping with sudo -S if needed."""
    if needs_sudo and sudo_password:
        full_cmd = f"sudo -S {cmd}"
        stdin_data = sudo_password + "\n"
    else:
        full_cmd = cmd
        stdin_data = None

    coro = conn.run(full_cmd, input=stdin_data, check=False)
    if timeout:
        return await asyncio.wait_for(coro, timeout=timeout)
    return await coro


async def _detect_pm(
    conn: asyncssh.SSHClientConnection, sudo_password: str | None, needs_sudo: bool
):
    result = await _run(
        conn, DETECT_CMD, sudo_password=sudo_password, needs_sudo=needs_sudo
    )
    return get_package_manager(result.stdout.strip())


async def verify_connection(
    host: dict, creds: dict | None = None
) -> dict:
    """Returns {"ok": bool, "message": str}."""
    h = host["host"]
    user = (host.get("user") or "").strip()
    log.info("SSH: testing connection to %s as %s", h, user)
    try:
        async with await _connect(host, creds) as conn:
            result = await conn.run("echo ok", check=False)
        if result.stdout.strip() == "ok":
            log.info("SSH: %s connected", h)
            return {"ok": True, "message": "Connected successfully."}
        msg = "Connected but command returned unexpected output."
        log.warning("SSH: %s failed — %s", h, msg)
        return {"ok": False, "message": msg}
    except Exception as exc:
        log.warning("SSH: %s failed — %s", h, exc)
        return {"ok": False, "message": str(exc)}


async def discover_containers(
    host: dict, creds: dict | None = None
) -> list[dict]:
    """Return list of running containers as [{"id": name, "name": name, "image": image}, ...].
    Returns empty list on error or if Docker not available."""
    creds = creds or {}
    needs_sudo = _needs_sudo(host)
    sudo_password = creds.get("sudo_password")
    try:
        async with await _connect(host, creds) as conn:
            result = await _run(
                conn,
                'docker ps --format \'{"name":"{{.Names}}","image":"{{.Image}}"}\'',
                sudo_password=sudo_password,
                needs_sudo=needs_sudo,
            )
        containers = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json

                obj = json.loads(line)
                name = obj.get("name", "")
                if name:
                    containers.append(
                        {"id": name, "name": name, "image": obj.get("image", "")}
                    )
            except Exception:
                pass
        return containers
    except Exception:
        return []


async def detect_docker_stacks(
    host: dict, creds: dict | None = None
) -> int:
    """Return the number of Docker Compose stacks found on the host, or -1 on error."""
    creds = creds or {}
    needs_sudo = _needs_sudo(host)
    sudo_password = creds.get("sudo_password")
    try:
        async with await _connect(host, creds) as conn:
            result = await _run(
                conn,
                "docker compose ls --all --format json 2>/dev/null || echo '[]'",
                sudo_password=sudo_password,
                needs_sudo=needs_sudo,
            )
        import json

        stacks = json.loads(result.stdout.strip() or "[]")
        return len(stacks) if isinstance(stacks, list) else 0
    except Exception:
        return -1


async def check_host_updates(
    host: dict, creds: dict | None = None
) -> dict:
    """
    Returns:
      {
        "packages": [{"name": str, "current": str, "available": str}, ...],
        "reboot_required": bool,
        "package_manager": str,
      }
    """
    from .config_manager import get_update_check_ttl_minutes
    from .update_check_cache import is_cache_fresh, mark_refreshed

    h = host["host"]
    log.info("SSH: checking %s for OS updates", h)
    creds = creds or {}
    use_sudo = _needs_sudo(host)
    sudo_password = creds.get("sudo_password")

    cache_key = f"ssh:{host.get('slug') or h}"
    ttl = get_update_check_ttl_minutes()
    refresh = not is_cache_fresh(cache_key, ttl)

    async with await _connect(host, creds) as conn:
        pm = await asyncio.wait_for(
            _detect_pm(conn, sudo_password, use_sudo), timeout=_CHECK_TIMEOUT
        )
        result = await _run(
            conn,
            pm.list_cmd(refresh=refresh),
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
            timeout=_CHECK_TIMEOUT,
        )
    if refresh:
        mark_refreshed(cache_key)

    packages, reboot_required = pm.parse(result.stdout)
    n = len(packages)
    if n:
        log.info("SSH: %s — %d update(s) available", h, n)
    else:
        log.info("SSH: %s — up to date", h)
    return {
        "packages": packages,
        "reboot_required": reboot_required,
        "package_manager": pm.name,
    }


async def reboot_host(
    host: dict, creds: dict | None = None
) -> list[str]:
    """Schedules an immediate reboot and returns. The SSH connection will drop."""
    creds = creds or {}
    use_sudo = _needs_sudo(host)
    sudo_password = creds.get("sudo_password")

    async with await _connect(host, creds) as conn:
        await _run(
            conn,
            "nohup sh -c 'sleep 2 && reboot' >/dev/null 2>&1 &",
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
        )
    return ["Reboot initiated — server will be back in ~30 seconds."]


async def run_host_update_buffered(
    host: dict, creds: dict | None = None
) -> list[str]:
    """Detects the package manager, runs the appropriate upgrade command, returns all output lines."""
    h = host["host"]
    log.info("SSH: running upgrade on %s", h)
    creds = creds or {}
    use_sudo = _needs_sudo(host)
    sudo_password = creds.get("sudo_password")

    async with await _connect(host, creds) as conn:
        pm = await _detect_pm(conn, sudo_password, use_sudo)
        result = await _run(
            conn,
            pm.upgrade_cmd(),
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
            timeout=_COMMAND_TIMEOUT,
        )

    lines = result.stdout.splitlines()
    if result.returncode != 0:
        if result.stderr:
            lines += result.stderr.splitlines()
        log.error("SSH: upgrade failed on %s (exit %s)", h, result.returncode)
    else:
        log.info("SSH: upgrade complete on %s", h)
    return lines
