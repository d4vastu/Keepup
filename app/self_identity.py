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
