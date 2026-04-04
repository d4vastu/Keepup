"""
Centralised backend initialisation.

Both the app startup and admin save routes call reload_backends() so that
connection changes take effect immediately without a container restart.
"""
import os

from .config_manager import get_dockerhub_config, get_portainer_config
from .credentials import get_integration_credentials

_backends: list = []


def get_backends() -> list:
    return _backends


def get_dockerhub_creds() -> dict | None:
    """Read DockerHub creds from store, fall back to env vars."""
    dh_cfg = get_dockerhub_config()
    dh_creds = get_integration_credentials("dockerhub")
    user = dh_cfg.get("username") or os.getenv("DOCKERHUB_USERNAME", "")
    token = dh_creds.get("token") or os.getenv("DOCKERHUB_TOKEN", "")
    return {"username": user, "token": token} if user and token else None


async def reload_backends() -> list:
    """
    Build the backends list from config + credential store (env var fallback).
    Updates all modules that hold a reference to the backends list.
    """
    global _backends
    backends = []

    # Portainer — credential store first, env var fallback
    port_cfg = get_portainer_config()
    port_creds = get_integration_credentials("portainer")
    url = port_cfg.get("url") or os.getenv("PORTAINER_URL", "")
    key = port_creds.get("api_key") or os.getenv("PORTAINER_API_KEY", "")
    verify_ssl = port_cfg.get("verify_ssl", False) or (
        os.getenv("PORTAINER_VERIFY_SSL", "false").lower() == "true"
    )

    if url and key:
        from .portainer_client import PortainerClient
        from .backends import PortainerBackend
        backends.append(PortainerBackend(PortainerClient(url=url, api_key=key, verify_ssl=verify_ssl)))

    from .backends import SSHDockerBackend
    backends.append(SSHDockerBackend())

    _backends = backends

    # Propagate to modules that cache the list
    from .auto_update_scheduler import set_backends
    from .auto_updates_router import set_backends as set_auto_updates_backends
    set_backends(_backends)
    set_auto_updates_backends(_backends)

    return _backends
