import asyncio
import asyncssh


async def _connect(host: dict, ssh_cfg: dict) -> asyncssh.SSHClientConnection:
    return await asyncssh.connect(
        host["host"],
        port=host.get("port", ssh_cfg.get("default_port", 22)),
        username=host.get("user", ssh_cfg.get("default_user", "root")),
        client_keys=[host.get("key", ssh_cfg.get("default_key"))],
        known_hosts=None,
        connect_timeout=ssh_cfg.get("connect_timeout", 15),
    )


async def check_host_updates(host: dict, ssh_cfg: dict) -> dict:
    """
    Returns:
      {
        "packages": [{"name": str, "current": str, "available": str}, ...],
        "reboot_required": bool,
      }
    """
    async with await _connect(host, ssh_cfg) as conn:
        result = await conn.run(
            "apt-get update -qq 2>/dev/null;"
            " apt list --upgradable 2>/dev/null;"
            " echo '__REBOOT__';"
            " [ -f /var/run/reboot-required ] && echo yes || echo no",
            check=False,
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
        # Format: "nginx/stable 1.26.0-1 amd64 [upgradable from: 1.24.0-1]"
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


async def reboot_host(host: dict, ssh_cfg: dict) -> list[str]:
    """Schedules an immediate reboot and returns. The SSH connection will drop."""
    async with await _connect(host, ssh_cfg) as conn:
        await conn.run(
            "nohup sh -c 'sleep 2 && reboot' >/dev/null 2>&1 &",
            check=False,
        )
    return ["Reboot initiated — server will be back in ~30 seconds."]


async def run_host_update_buffered(host: dict, ssh_cfg: dict) -> list[str]:
    """
    Runs apt-get upgrade and returns all output lines when complete.
    Used by background job runner.
    """
    cmd = "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1"
    timeout = ssh_cfg.get("command_timeout", 600)

    async with await _connect(host, ssh_cfg) as conn:
        result = await asyncio.wait_for(
            conn.run(cmd, check=False),
            timeout=timeout,
        )

    lines = result.stdout.splitlines()
    if result.returncode != 0 and result.stderr:
        lines += result.stderr.splitlines()
    return lines
