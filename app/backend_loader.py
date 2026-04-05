"""
Centralised backend initialisation.

Both the app startup and admin save routes call reload_backends() so that
connection changes take effect immediately without a container restart.
"""

from .config_manager import get_dockerhub_config, get_portainer_config
from .credentials import get_integration_credentials

_backends: list = []


def get_backends() -> list:
    return _backends


def get_dockerhub_creds() -> dict | None:
    """Read DockerHub credentials from the UI-managed credential store."""
    dh_cfg = get_dockerhub_config()
    dh_creds = get_integration_credentials("dockerhub")
    user = dh_cfg.get("username", "")
    token = dh_creds.get("token", "")
    return {"username": user, "token": token} if user and token else None


async def reload_backends() -> list:
    """
    Build the backends list from config + credential store.
    Updates all modules that hold a reference to the backends list.
    """
    global _backends
    backends = []

    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    url = port_cfg.get("url", "")
    key = port_creds.get("api_key", "")
    verify_ssl = port_cfg.get("verify_ssl", False)

    if url and key:
        from .portainer_client import PortainerClient
        from .backends import PortainerBackend

        backends.append(
            PortainerBackend(
                PortainerClient(url=url, api_key=key, verify_ssl=verify_ssl)
            )
        )

    from .backends import SSHDockerBackend

    backends.append(SSHDockerBackend())

    _backends = backends

    # Propagate to modules that cache the list
    from .auto_update_scheduler import set_backends
    from .auto_updates_router import set_backends as set_auto_updates_backends

    set_backends(_backends)
    set_auto_updates_backends(_backends)

    return _backends
