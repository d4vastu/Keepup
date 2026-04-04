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


def _build_host_entry(name: str, host: str, user: str | None, port: int | None,
                      key: str | None, password: str | None) -> dict:
    entry: dict = {"name": name, "host": host}
    if user:
        entry["user"] = user
    if port:
        entry["port"] = port
    if password:
        entry["password"] = password
    elif key:
        entry["key"] = key
    return entry


def add_host(name: str, host: str, user: str | None, port: int | None,
             key: str | None, password: str | None = None) -> None:
    config = load_config()
    hosts = config.setdefault("hosts", [])
    hosts.append(_build_host_entry(name, host, user, port, key, password))
    save_config(config)


def update_host(slug: str, name: str, host: str, user: str | None, port: int | None,
                key: str | None, password: str | None = None) -> None:
    config = load_config()
    hosts = config.get("hosts", [])
    for i, h in enumerate(hosts):
        if slugify(h["name"]) == slug:
            hosts[i] = _build_host_entry(name, host, user, port, key, password)
            break
    save_config(config)


def delete_host(slug: str) -> None:
    config = load_config()
    config["hosts"] = [h for h in config.get("hosts", []) if slugify(h["name"]) != slug]
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
