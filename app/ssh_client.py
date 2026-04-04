import asyncio

import asyncssh


def _needs_sudo(host: dict, ssh_cfg: dict) -> bool:
    user = host.get("user") or ssh_cfg.get("default_user", "root")
    return user != "root"


async def _connect(host: dict, ssh_cfg: dict, creds: dict | None = None) -> asyncssh.SSHClientConnection:
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
        # Fall back to key file (legacy / existing setups)
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


async def verify_connection(host: dict, ssh_cfg: dict, creds: dict | None = None) -> dict:
    """Returns {"ok": bool, "message": str}."""
    try:
        async with await _connect(host, ssh_cfg, creds) as conn:
            result = await conn.run("echo ok", check=False)
        if result.stdout.strip() == "ok":
            return {"ok": True, "message": "Connected successfully."}
        return {"ok": False, "message": "Connected but command returned unexpected output."}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


async def check_host_updates(
    host: dict, ssh_cfg: dict, creds: dict | None = None
) -> dict:
    """
    Returns:
      {
        "packages": [{"name": str, "current": str, "available": str}, ...],
        "reboot_required": bool,
      }
    """
    creds = creds or {}
    use_sudo = _needs_sudo(host, ssh_cfg)
    sudo_password = creds.get("sudo_password")

    async with await _connect(host, ssh_cfg, creds) as conn:
        result = await _run(
            conn,
            "sh -c 'apt-get update -qq 2>/dev/null;"
            " apt list --upgradable 2>/dev/null;"
            " echo __REBOOT__;"
            " [ -f /var/run/reboot-required ] && echo yes || echo no'",
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
        )

    output = result.stdout
    reboot_required = False

    if "__REBOOT__" in output:
        apt_part, reboot_part = output.split("__REBOOT__", 1)
        reboot_required = reboot_part.strip().startswith("yes")
    else:
        apt_part = output

    packages = []
    for line in apt_part.splitlines():
        if "[upgradable from:" not in line:
            continue
        try:
            name = line.split("/")[0]
            parts = line.split()
            available = parts[1] if len(parts) > 1 else "?"
            current = parts[-1].rstrip("]") if len(parts) > 3 else "?"
            packages.append({"name": name, "current": current, "available": available})
        except Exception:
            continue

    return {"packages": packages, "reboot_required": reboot_required}


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
    """Runs apt-get upgrade and returns all output lines when complete."""
    creds = creds or {}
    use_sudo = _needs_sudo(host, ssh_cfg)
    sudo_password = creds.get("sudo_password")
    timeout = ssh_cfg.get("command_timeout", 600)

    async with await _connect(host, ssh_cfg, creds) as conn:
        result = await _run(
            conn,
            "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1",
            sudo_password=sudo_password,
            needs_sudo=use_sudo,
            timeout=timeout,
        )

    lines = result.stdout.splitlines()
    if result.returncode != 0 and result.stderr:
        lines += result.stderr.splitlines()
    return lines
