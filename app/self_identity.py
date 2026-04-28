import os
import re

_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_CGROUP_LONG_ID_RE = re.compile(r"[0-9a-f]{64}")


def _container_id_from_cgroup() -> str | None:
    """Read the container ID from /proc/self/cgroup (works when HOSTNAME is overridden).

    Covers both cgroups v1 (/docker/<64-char-id>) and v2 (docker-<64-char-id>.scope).
    Returns None outside Docker or when the file is unavailable.
    """
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                m = _CGROUP_LONG_ID_RE.search(line)
                if m:
                    return m.group(0)[:12]
    except OSError:
        pass
    return None


def get_self_container_id() -> str | None:
    """Return the short container ID when Keepup runs inside Docker, else None.

    Tries HOSTNAME first (Docker default). Falls back to /proc/self/cgroup so
    the ID is found even when the user sets a custom hostname: in docker-compose.
    Returns None on bare-metal or in environments where neither source is available.
    """
    hostname = os.environ.get("HOSTNAME", "")
    if _CONTAINER_ID_RE.match(hostname):
        return hostname
    return _container_id_from_cgroup()


def get_self_container_name() -> str | None:
    """Return the container name from KEEPUP_CONTAINER_NAME env var, else None.

    Used as a name-based fallback for self-exclusion when the container ID cannot
    be determined (e.g. some container runtimes that don't expose cgroup paths).
    Set KEEPUP_CONTAINER_NAME=keepup in docker-compose.yml to enable this.
    """
    name = os.environ.get("KEEPUP_CONTAINER_NAME", "").strip()
    return name or None


def is_self_on_proxmox_node(node_slug: str) -> bool:
    """Return True if KEEPUP_PROXMOX_NODE env var matches node_slug.

    Set KEEPUP_PROXMOX_NODE to the Proxmox node name where Keepup is running
    to prevent rebooting the node from under itself.
    """
    self_node = os.environ.get("KEEPUP_PROXMOX_NODE", "")
    return bool(self_node) and self_node == node_slug
