import os
import re

_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def get_self_container_id() -> str | None:
    """Return the short container ID when Keepup runs inside Docker, else None.

    Docker sets HOSTNAME to the 12-char hex short ID by default. Returns None
    on bare-metal or in test environments where HOSTNAME looks like a real hostname.
    """
    hostname = os.environ.get("HOSTNAME", "")
    if _CONTAINER_ID_RE.match(hostname):
        return hostname
    return None


def is_self_on_proxmox_node(node_slug: str) -> bool:
    """Return True if KEEPUP_PROXMOX_NODE env var matches node_slug.

    Set KEEPUP_PROXMOX_NODE to the Proxmox node name where Keepup is running
    to prevent rebooting the node from under itself.
    """
    self_node = os.environ.get("KEEPUP_PROXMOX_NODE", "")
    return bool(self_node) and self_node == node_slug
