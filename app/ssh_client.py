import asyncio

import asyncssh

from .package_managers import DETECT_CMD, get_package_manager


def _needs_sudo(host: dict, ssh_cfg: dict) -> bool:
    user = host.get("user") or ssh_cfg.get("default_user", "root")
    return user != "root"


async def _connect(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> asyncssh.SSHClientConnection:
    creds = creds or {}
    kwargs: dict = {
        "host": host["host"],
        "port": host.get("port", ssh_cfg.get("default_port", 22)),
        "username": host.get("user", ssh_cfg.get("default_user", "root")),
        "known_hosts": None,
        "connect_timeout": ssh_cfg.get("connect_timeout", 15),
    }
    if creds.get("ssh_password"):
        kwargs["password"] = creds["ssh_password"]
        kwargs["preferred_auth"] = "password"
    elif creds.get("ssh_key"):
        key = asyncssh.import_private_key(creds["ssh_key"])
        kwargs["client_keys"] = [key]
    else:
        key_path = host.get("key", ssh_cfg.get("default_key"))
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
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> dict:
    """Returns {"ok": bool, "message": str}."""
    try:
        async with await _connect(host, ssh_cfg, creds) as conn:
            result = await conn.run("echo ok", check=False)
        if result.stdout.strip() == "ok":
            return {"ok": True, "message": "Connected successfully."}
        return {
            "ok": False,
            "message": "Connected but command returned unexpected output.",
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


async def discover_containers(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> list[dict]:
    """Return list of running containers as [{"id": name, "name": name, "image": image}, ...].
    Returns empty list on error or if Docker not available."""
    try:
        async with await _connect(host, ssh_cfg, creds) as conn:
            result = await conn.run(
                'docker ps --format \'{"name":"{{.Names}}","image":"{{.Image}}"}\'',
                check=False,
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
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> int:
    """Return the number of Docker Compose stacks found on the host, or -1 on error."""
    try:
        async with await _connect(host, ssh_cfg, creds) as conn:
            result = await conn.run(
                "docker compose ls --all --format json 2>/dev/null || echo '[]'",
                check=False,
            )
        import json

        stacks = json.loads(result.stdout.strip() or "[]")
        return len(stacks) if isinstance(stacks, list) else 0
    except Exception:
        return -1


async def check_host_updates(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> dict:
    """
    Returns:
      {
        "packages": [{"name": str, "current": str, "available": str}, ...],
        "reboot_required": bool,
        "package_manager": str,
      }
    """
    creds = creds or {}
    use_sudo = _needs_sudo(host, ssh_cfg)
    sudo_password = creds.get("sudo_password")

    async with await _connect(host, ssh_cfg, creds) as conn:
        pm = await _detect_pm(conn, sudo_password, use_sudo)
        result = await _run(
            conn,
            pm.list_cmd(),
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
        )

    packages, reboot_required = pm.parse(result.stdout)
    return {
        "packages": packages,
        "reboot_required": reboot_required,
        "package_manager": pm.name,
    }


async def reboot_host(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> list[str]:
    """Schedules an immediate reboot and returns. The SSH connection will drop."""
    creds = creds or {}
    use_sudo = _needs_sudo(host, ssh_cfg)
    sudo_password = creds.get("sudo_password")

    async with await _connect(host, ssh_cfg, creds) as conn:
        await _run(
            conn,
            "nohup sh -c 'sleep 2 && reboot' >/dev/null 2>&1 &",
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
        )
    return ["Reboot initiated — server will be back in ~30 seconds."]


async def run_host_update_buffered(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> list[str]:
    """Detects the package manager, runs the appropriate upgrade command, returns all output lines."""
    creds = creds or {}
    use_sudo = _needs_sudo(host, ssh_cfg)
    sudo_password = creds.get("sudo_password")
    timeout = ssh_cfg.get("command_timeout", 600)

    async with await _connect(host, ssh_cfg, creds) as conn:
        pm = await _detect_pm(conn, sudo_password, use_sudo)
        result = await _run(
            conn,
            pm.upgrade_cmd(),
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
            timeout=timeout,
        )

    lines = result.stdout.splitlines()
    if result.returncode != 0 and result.stderr:
        lines += result.stderr.splitlines()
    return lines
