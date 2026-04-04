import os
import threading
from pathlib import Path

import yaml

_CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.yml"))
_lock = threading.Lock()


def slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("_", "-")


def load_config() -> dict:
    with _lock:
        if not _CONFIG_PATH.exists():
            return {"hosts": [], "ssh": {}}
        return yaml.safe_load(_CONFIG_PATH.read_text()) or {}


def save_config(config: dict) -> None:
    with _lock:
        _CONFIG_PATH.write_text(
            yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

def get_hosts() -> list[dict]:
    config = load_config()
    hosts = config.get("hosts", []) or []
    return [
        {**h, "slug": slugify(h["name"])}
        for h in hosts
        if "XXX" not in str(h.get("host", ""))
    ]


def _build_host_entry(name: str, host: str, user: str | None, port: int | None) -> dict:
    """Builds a host entry for config.yml — no credentials stored here."""
    entry: dict = {"name": name, "host": host}
    if user:
        entry["user"] = user
    if port:
        entry["port"] = port
    return entry


def add_host(name: str, host: str, user: str | None, port: int | None) -> str:
    """Add a host to config and return its slug."""
    config = load_config()
    hosts = config.setdefault("hosts", [])
    hosts.append(_build_host_entry(name, host, user, port))
    save_config(config)
    return slugify(name)


def update_host(slug: str, name: str, host: str, user: str | None, port: int | None) -> str:
    """Update a host entry and return the new slug (may differ if name changed)."""
    config = load_config()
    hosts = config.get("hosts", [])
    for i, h in enumerate(hosts):
        if slugify(h["name"]) == slug:
            entry = _build_host_entry(name, host, user, port)
            # Preserve docker monitoring settings across renames
            for key in ("docker_mode", "docker_stacks"):
                if key in h:
                    entry[key] = h[key]
            hosts[i] = entry
            break
    save_config(config)
    return slugify(name)


def delete_host(slug: str) -> None:
    config = load_config()
    config["hosts"] = [h for h in config.get("hosts", []) if slugify(h["name"]) != slug]
    save_config(config)


# ---------------------------------------------------------------------------
# Docker monitoring settings
# ---------------------------------------------------------------------------

def set_docker_monitoring(
    slug: str,
    mode: str,  # "all" | "all_and_new" | "selected" | "none"
    stacks: list[str] | None = None,
) -> None:
    """Configure Docker Compose monitoring for a host."""
    config = load_config()
    for h in config.get("hosts", []):
        if slugify(h["name"]) == slug:
            if mode == "none":
                h.pop("docker_mode", None)
                h.pop("docker_stacks", None)
            else:
                h["docker_mode"] = mode
                if mode == "selected" and stacks is not None:
                    h["docker_stacks"] = stacks
                else:
                    h.pop("docker_stacks", None)
            break
    save_config(config)


# ---------------------------------------------------------------------------
# SSH settings
# ---------------------------------------------------------------------------

def get_ssh_config() -> dict:
    return load_config().get("ssh", {})


def update_ssh_config(
    default_user: str,
    default_port: int,
    default_key: str,
    connect_timeout: int,
    command_timeout: int,
) -> None:
    config = load_config()
    config["ssh"] = {
        "default_key": default_key,
        "default_user": default_user,
        "default_port": default_port,
        "connect_timeout": connect_timeout,
        "command_timeout": command_timeout,
    }
    save_config(config)
