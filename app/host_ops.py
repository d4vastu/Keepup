"""
Type-aware OS-update and reboot dispatch.

A monitored host can be one of four kinds, and each needs different handling
for upgrades and reboots:

  - ``plain``  — an ordinary Linux host reached over SSH.
  - ``node``   — a Proxmox VE node. Upgraded over SSH (it is a Debian host),
                 but rebooted through the Proxmox API so guests are stopped
                 gracefully first instead of being killed by a hard reboot.
  - ``lxc``    — a Proxmox LXC container. Upgraded via ``pct exec`` and
                 rebooted via the Proxmox API; no in-container SSH required.
  - ``vm``     — a Proxmox VM. Has its own kernel + SSH, so it behaves like a
                 ``plain`` host for both upgrade and reboot.

Both the manual dashboard routes (``main``) and the unattended scheduler
(``auto_update_scheduler``) dispatch through this single module so the two
paths can never drift apart again (OP#176).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from .config_manager import get_proxmox_config
from .credentials import get_integration_credentials
from .proxmox_client import client_from_config
from .self_identity import is_self_on_proxmox_node
from .ssh_client import check_host_updates, reboot_host, run_host_update_buffered

log = logging.getLogger(__name__)


def classify_host(host: dict) -> str:
    """Return one of ``"plain"``, ``"node"``, ``"lxc"``, ``"vm"`` for a host."""
    node = host.get("proxmox_node")
    if not node:
        return "plain"
    if host.get("proxmox_vmid") is None:
        return "node"
    return "vm" if (host.get("proxmox_type") or "lxc") == "vm" else "lxc"


async def build_proxmox_client():
    """Construct a configured ProxmoxClient (thin wrapper, patchable in tests)."""
    return client_from_config()


def _lxc_ssh_context(host: dict) -> tuple[str, dict]:
    """Resolve ``(ssh_host, ssh_creds)`` for reaching an LXC via its node.

    SSH targets the Proxmox node (whose hostname comes from the integration
    URL); credentials come from the stored Proxmox integration creds. Mirrors
    the manual ``_job_run_lxc_upgrade`` path.
    """
    px_cfg = get_proxmox_config()
    px_creds = get_integration_credentials("proxmox")
    ssh_host = urlparse(px_cfg.get("url", "")).hostname or host.get("host", "")
    ssh_creds = {
        "user": px_creds.get("ssh_user", "root"),
        "port": px_creds.get("ssh_port", 22),
        "key_path": px_creds.get("ssh_key_path", ""),
        "ssh_password": px_creds.get("ssh_password", ""),
    }
    return ssh_host, ssh_creds


async def run_os_update(host: dict, creds: dict) -> list[str]:
    """Run the OS upgrade for ``host`` using the right mechanism for its kind."""
    if classify_host(host) == "lxc":
        client = await build_proxmox_client()
        ssh_host, ssh_creds = _lxc_ssh_context(host)
        return await client.upgrade_lxc(
            host["proxmox_node"], host["proxmox_vmid"], ssh_host, ssh_creds
        )
    return await run_host_update_buffered(host, creds)


async def reboot_required_typed(host: dict, creds: dict) -> bool:
    """Return whether ``host`` needs a reboot, queried the right way per kind."""
    kind = classify_host(host)
    if kind == "node":
        client = await build_proxmox_client()
        return await client.get_node_reboot_required(host["proxmox_node"])
    if kind == "lxc":
        # LXCs share the host kernel — an LXC-level reboot is never required.
        return False
    check = await check_host_updates(host, creds)
    return bool(check.get("reboot_required"))


async def reboot_host_typed(host: dict, creds: dict) -> list[str]:
    """Reboot ``host`` using the right mechanism, with the self-reboot guard."""
    kind = classify_host(host)
    if kind == "node":
        if is_self_on_proxmox_node(host["proxmox_node"]):
            raise ValueError(
                f"Refusing self-reboot: Keepup runs on Proxmox node "
                f"{host['proxmox_node']!r}"
            )
        client = await build_proxmox_client()
        await client.reboot_node(host["proxmox_node"])
        return ["Reboot initiated via Proxmox API — guests are stopped first."]
    if kind == "lxc":
        client = await build_proxmox_client()
        await client.reboot_lxc(host["proxmox_node"], host["proxmox_vmid"])
        return ["LXC reboot initiated via Proxmox API."]
    return await reboot_host(host, creds)
