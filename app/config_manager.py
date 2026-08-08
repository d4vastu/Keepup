import os
import threading
from pathlib import Path

import yaml

_CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.yml"))
_lock = threading.Lock()


def derive_api_user(token_id: str) -> str:
    """Derive the Proxmox API user from a Token ID (format: user@realm!token_name)."""
    return token_id.split("!")[0] if "!" in token_id else token_id


def slugify(name: str) -> str:
    import re
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def load_config() -> dict:
    with _lock:
        if not _CONFIG_PATH.exists():
            return {"hosts": []}
        return yaml.safe_load(_CONFIG_PATH.read_text()) or {}


def save_config(config: dict) -> None:
    with _lock:
        _CONFIG_PATH.write_text(
            yaml.dump(
                config, default_flow_style=False, allow_unicode=True, sort_keys=False
            )
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


def _build_host_entry(
    name: str,
    host: str,
    user: str | None,
    port: int | None,
    key_path: str | None = None,
    docker_mode: str | None = None,
    proxmox_node: str | None = None,
    proxmox_vmid: int | None = None,
    proxmox_type: str | None = None,
) -> dict:
    """Builds a host entry for config.yml — no credentials stored here."""
    entry: dict = {"name": name, "host": host}
    if user:
        entry["user"] = user
    if port:
        entry["port"] = port
    if key_path:
        entry["key"] = key_path
    if docker_mode and docker_mode != "none":
        entry["docker_mode"] = docker_mode
    if proxmox_node:
        entry["proxmox_node"] = proxmox_node
    if proxmox_vmid is not None:
        entry["proxmox_vmid"] = proxmox_vmid
    if proxmox_type:
        entry["proxmox_type"] = proxmox_type
    return entry


def add_host(
    name: str,
    host: str,
    user: str | None,
    port: int | None,
    key_path: str | None = None,
    docker_mode: str | None = None,
    proxmox_node: str | None = None,
    proxmox_vmid: int | None = None,
    proxmox_type: str | None = None,
) -> str:
    """Add a host to config and return its slug."""
    config = load_config()
    hosts = config.setdefault("hosts", [])
    hosts.append(
        _build_host_entry(
            name, host, user, port,
            key_path=key_path,
            docker_mode=docker_mode,
            proxmox_node=proxmox_node,
            proxmox_vmid=proxmox_vmid,
            proxmox_type=proxmox_type,
        )
    )
    save_config(config)
    return slugify(name)


def update_host(
    slug: str, name: str, host: str, user: str | None, port: int | None
) -> str:
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
    containers: list[str] | None = None,
) -> None:
    """Configure container monitoring for a host."""
    config = load_config()
    for h in config.get("hosts", []):
        if slugify(h["name"]) == slug:
            if mode == "none":
                h.pop("docker_mode", None)
                h.pop("docker_containers", None)
                h.pop("docker_stacks", None)
            else:
                h["docker_mode"] = mode
                if mode == "selected" and containers is not None:
                    h["docker_containers"] = containers
                else:
                    h.pop("docker_containers", None)
                h.pop("docker_stacks", None)
            break
    save_config(config)


def save_wizard_container_selection(selections: list[str]) -> None:
    """Save wizard container selections (list of 'slug:container_name' values).

    For each host slug, set docker_mode='selected' with the chosen containers.
    Hosts with no containers selected get docker_mode cleared.
    """
    # Group by slug
    by_slug: dict[str, list[str]] = {}
    for item in selections:
        if ":" in item:
            slug, container = item.split(":", 1)
            by_slug.setdefault(slug, []).append(container)

    config = load_config()
    for h in config.get("hosts", []):
        slug = slugify(h["name"])
        if slug in by_slug:
            h["docker_mode"] = "selected"
            h["docker_containers"] = by_slug[slug]
            h.pop("docker_stacks", None)
        # Hosts not in selections are left unchanged (user may not have docker)
    save_config(config)


# ---------------------------------------------------------------------------
# SSH settings migration
# ---------------------------------------------------------------------------


def migrate_ssh_config() -> int:
    """One-time migration: copy global SSH defaults into per-host records, then remove them.

    Returns the number of hosts that were left without an SSH user (need manual
    assignment before they can be checked or updated).
    """
    config = load_config()
    ssh = config.get("ssh", {})
    if not ssh:
        return sum(1 for h in config.get("hosts", []) if not h.get("user"))

    default_port = ssh.get("default_port")
    changed = False
    for host in config.get("hosts", []):
        if default_port and not host.get("port"):
            host["port"] = default_port
            changed = True

    # Remove the deprecated global SSH block entirely.
    config.pop("ssh", None)
    if changed or "ssh" in config:
        save_config(config)

    return sum(1 for h in config.get("hosts", []) if not h.get("user"))


# ---------------------------------------------------------------------------
# Integration settings (non-sensitive — URLs/usernames only)
# ---------------------------------------------------------------------------


def get_portainer_config() -> dict:
    return load_config().get("portainer", {})


def save_portainer_config(
    url: str,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
) -> None:
    config = load_config()
    if url:
        existing = config.get("portainer", {})
        pem = pinned_cert_pem or existing.get("pinned_cert_pem", "")
        fp = pinned_fingerprint or existing.get("pinned_fingerprint", "")
        entry: dict = {"url": url.rstrip("/")}
        if pem:
            entry["pinned_cert_pem"] = pem
            entry["pinned_fingerprint"] = fp
        config["portainer"] = entry
    else:
        config.pop("portainer", None)
    save_config(config)


def get_pushover_config() -> dict:
    return load_config().get("pushover", {})


def save_pushover_config(enabled: bool) -> None:
    cfg = load_config()
    cfg["pushover"] = {"enabled": enabled}
    save_config(cfg)


def get_email_config() -> dict:
    return load_config().get("email", {})


def save_email_config(
    sender_name: str,
    sender_address: str,
    recipient_address: str,
    smtp_host: str,
    smtp_port: int,
    tls: bool,
) -> None:
    cfg = load_config()
    cfg["email"] = {
        "sender_name": sender_name,
        "sender_address": sender_address,
        "recipient_address": recipient_address,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "tls": tls,
    }
    save_config(cfg)


def get_timezone() -> str:
    return load_config().get("timezone", "UTC")


def save_timezone(tz: str) -> None:
    config = load_config()
    config["timezone"] = tz
    save_config(config)


_UPDATE_CHECK_SCHEDULES = {
    "6h": "0 */6 * * *",
    "12h": "0 */12 * * *",
    "24h": "0 2 * * *",
    "manual": "",
}


def get_update_check_schedule() -> str:
    """Return the update check schedule key ('6h', '12h', '24h', or 'manual')."""
    cron = load_config().get("update_check_schedule", "")
    for key, val in _UPDATE_CHECK_SCHEDULES.items():
        if val == cron:
            return key
    return "manual" if not cron else "manual"


def save_update_check_schedule(schedule_key: str) -> None:
    config = load_config()
    cron = _UPDATE_CHECK_SCHEDULES.get(schedule_key, "")
    if cron:
        config["update_check_schedule"] = cron
    else:
        config.pop("update_check_schedule", None)
    save_config(config)


def get_dockerhub_config() -> dict:
    return load_config().get("dockerhub", {})


def save_dockerhub_config(username: str) -> None:
    config = load_config()
    if username:
        config["dockerhub"] = {"username": username}
    else:
        config.pop("dockerhub", None)
    save_config(config)


def get_proxmox_config() -> dict:
    """Return the singleton Proxmox block.

    Compatibility shim (OP#209): after the multi-server migration there is no
    ``proxmox:`` block, so this resolves to the *first* configured server minus
    its ``id``. Removed in OP#210, once every call site passes a server id.
    """
    config = load_config()
    legacy = config.get("proxmox")
    if legacy:
        return legacy
    servers = config.get("proxmox_servers") or []
    if servers:
        return {k: v for k, v in servers[0].items() if k != "id"}
    return {}


def save_proxmox_config(
    url: str,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
    verify_ssl: bool = True,
) -> None:
    """Write the singleton Proxmox block.

    Compatibility shim (OP#209): once ``proxmox_servers`` exists this writes
    through to the first server instead of recreating the legacy block, which
    the migration has already removed. Removed in OP#210.
    """
    config = load_config()
    if config.get("proxmox_servers") is not None and not config.get("proxmox"):
        _save_first_server(config, url, pinned_cert_pem, pinned_fingerprint, verify_ssl)
        return
    if url:
        existing = config.get("proxmox", {})
        pem = pinned_cert_pem or existing.get("pinned_cert_pem", "")
        fp = pinned_fingerprint or existing.get("pinned_fingerprint", "")
        entry: dict = {"url": url.rstrip("/"), "verify_ssl": verify_ssl}
        if pem:
            entry["pinned_cert_pem"] = pem
            entry["pinned_fingerprint"] = fp
        config["proxmox"] = entry
    else:
        config.pop("proxmox", None)
    save_config(config)


def _save_first_server(
    config: dict,
    url: str,
    pinned_cert_pem: str,
    pinned_fingerprint: str,
    verify_ssl: bool,
) -> None:
    """Back half of the :func:`save_proxmox_config` shim."""
    servers = config["proxmox_servers"]
    if not url:
        if servers:
            delete_proxmox_server(servers[0]["id"])
        return
    if not servers:
        add_proxmox_server(url, verify_ssl, pinned_cert_pem, pinned_fingerprint)
        return
    update_proxmox_server(
        servers[0]["id"],
        url=url,
        verify_ssl=verify_ssl,
        pinned_cert_pem=pinned_cert_pem or None,
        pinned_fingerprint=pinned_fingerprint or None,
    )


# ---------------------------------------------------------------------------
# Multi-server Proxmox (OP#209)
#
# A server's id is derived from its URL host and is used as an HTML id / CSS
# selector in the admin UI, so it must be CSS-safe ([a-z0-9-] only).
# ---------------------------------------------------------------------------

_PROXMOX_FIELDS = ("proxmox_node", "proxmox_vmid", "proxmox_type", "proxmox_server")


def slugify_server_id(url: str) -> str:
    """Derive a CSS-safe server id from a URL's host.

    ``slugify`` deletes every character outside ``[a-z0-9-]``, which would
    collapse ``192.168.1.10`` to ``1921681 10``-style digit soup. Dots and
    colons are the meaningful separators in a host, so map them to hyphens
    first. ``slugify`` itself is left alone — it derives host slugs, and
    changing it would silently rewrite every existing host's identity.
    """
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    return slugify(host.replace(".", "-").replace(":", "-")) or "proxmox"


def get_proxmox_servers() -> list[dict]:
    return load_config().get("proxmox_servers", []) or []


def get_proxmox_server(server_id: str) -> dict:
    """Return one server entry, or ``{}`` if the id is unknown."""
    for server in get_proxmox_servers():
        if server.get("id") == server_id:
            return server
    return {}


def _unique_server_id(base: str, taken: set[str]) -> str:
    """Disambiguate two URLs whose hosts slugify to the same id."""
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def add_proxmox_server(
    url: str,
    verify_ssl: bool = True,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
) -> str:
    """Append a server and return its derived id."""
    config = load_config()
    servers = config.setdefault("proxmox_servers", [])
    server_id = _unique_server_id(
        slugify_server_id(url), {s.get("id") for s in servers}
    )
    entry: dict = {
        "id": server_id,
        "url": url.rstrip("/"),
        "verify_ssl": verify_ssl,
    }
    if pinned_cert_pem:
        entry["pinned_cert_pem"] = pinned_cert_pem
        entry["pinned_fingerprint"] = pinned_fingerprint
    servers.append(entry)
    save_config(config)
    return server_id


def update_proxmox_server(
    server_id: str,
    url: str | None = None,
    verify_ssl: bool | None = None,
    pinned_cert_pem: str | None = None,
    pinned_fingerprint: str | None = None,
) -> None:
    """Update a server in place. ``None`` leaves a field unchanged.

    The id is deliberately *not* re-derived from a changed URL — host entries
    reference it, and rewriting it would orphan them.
    """
    config = load_config()
    for server in config.get("proxmox_servers", []) or []:
        if server.get("id") != server_id:
            continue
        if url is not None:
            server["url"] = url.rstrip("/")
        if verify_ssl is not None:
            server["verify_ssl"] = verify_ssl
        if pinned_cert_pem is not None:
            server["pinned_cert_pem"] = pinned_cert_pem
        if pinned_fingerprint is not None:
            server["pinned_fingerprint"] = pinned_fingerprint
        save_config(config)
        return


def delete_proxmox_server(server_id: str) -> None:
    """Remove a server, its credentials, and rewrite its hosts.

    Its node and LXC hosts are unusable without the API, so they go. Its VMs
    have their own kernel and SSH identity, so they survive as plain hosts with
    every ``proxmox_*`` field stripped.
    """
    from .credentials import delete_credentials, delete_integration_credentials

    config = load_config()
    servers = config.get("proxmox_servers", []) or []
    if not any(s.get("id") == server_id for s in servers):
        return
    config["proxmox_servers"] = [s for s in servers if s.get("id") != server_id]

    kept: list[dict] = []
    dropped: list[str] = []
    for host in config.get("hosts", []) or []:
        if host.get("proxmox_server") != server_id:
            kept.append(host)
            continue
        is_vm = (
            host.get("proxmox_vmid") is not None
            and host.get("proxmox_type") == "vm"
        )
        if is_vm:
            kept.append({k: v for k, v in host.items() if k not in _PROXMOX_FIELDS})
        else:
            dropped.append(slugify(host.get("name", "")))
    config["hosts"] = kept
    save_config(config)

    delete_integration_credentials(f"proxmox_{server_id}")
    for slug in dropped:
        if slug:
            delete_credentials(slug)


def migrate_proxmox_servers() -> str:
    """One-shot migration off the singleton ``proxmox:`` block.

    Returns the derived server id, or ``""`` when nothing was migrated.
    Follows the :func:`migrate_ssh_config` pattern and runs at startup.
    Deleting the legacy key is what makes it idempotent — there is no separate
    "migrated" flag to keep in sync.
    """
    from .credentials import rename_integration_credentials

    config = load_config()
    legacy = config.get("proxmox")
    if not legacy:
        return ""

    # Hand-edited config carrying both shapes: the new list is authoritative,
    # so drop the stale legacy block rather than merging a second server in.
    if config.get("proxmox_servers"):
        config.pop("proxmox", None)
        save_config(config)
        return ""

    url = legacy.get("url", "")
    server_id = slugify_server_id(url)
    entry: dict = {
        "id": server_id,
        "url": url,
        "verify_ssl": legacy.get("verify_ssl", True),
    }
    if legacy.get("pinned_cert_pem"):
        entry["pinned_cert_pem"] = legacy["pinned_cert_pem"]
        entry["pinned_fingerprint"] = legacy.get("pinned_fingerprint", "")
    config["proxmox_servers"] = [entry]

    for host in config.get("hosts", []) or []:
        if host.get("proxmox_node"):
            host["proxmox_server"] = server_id

    config.pop("proxmox", None)
    save_config(config)

    rename_integration_credentials("proxmox", f"proxmox_{server_id}")
    return server_id


def get_pbs_config() -> dict:
    return load_config().get("proxmox_backup", {})


def save_pbs_config(
    url: str,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
    verify_ssl: bool = True,
) -> None:
    config = load_config()
    if url:
        existing = config.get("proxmox_backup", {})
        pem = pinned_cert_pem or existing.get("pinned_cert_pem", "")
        fp = pinned_fingerprint or existing.get("pinned_fingerprint", "")
        entry: dict = {"url": url.rstrip("/"), "verify_ssl": verify_ssl}
        if pem:
            entry["pinned_cert_pem"] = pem
            entry["pinned_fingerprint"] = fp
        config["proxmox_backup"] = entry
    else:
        config.pop("proxmox_backup", None)
    save_config(config)


def get_opnsense_config() -> dict:
    return load_config().get("opnsense", {})


def save_opnsense_config(
    url: str,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
    verify_ssl: bool = True,
) -> None:
    config = load_config()
    if url:
        existing = config.get("opnsense", {})
        pem = pinned_cert_pem or existing.get("pinned_cert_pem", "")
        fp = pinned_fingerprint or existing.get("pinned_fingerprint", "")
        entry: dict = {"url": url.rstrip("/"), "verify_ssl": verify_ssl}
        if pem:
            entry["pinned_cert_pem"] = pem
            entry["pinned_fingerprint"] = fp
        config["opnsense"] = entry
    else:
        config.pop("opnsense", None)
    save_config(config)


def get_pfsense_config() -> dict:
    return load_config().get("pfsense", {})


def save_pfsense_config(
    url: str,
    pinned_cert_pem: str = "",
    pinned_fingerprint: str = "",
    verify_ssl: bool = True,
) -> None:
    config = load_config()
    if url:
        existing = config.get("pfsense", {})
        pem = pinned_cert_pem or existing.get("pinned_cert_pem", "")
        fp = pinned_fingerprint or existing.get("pinned_fingerprint", "")
        entry: dict = {"url": url.rstrip("/"), "verify_ssl": verify_ssl}
        if pem:
            entry["pinned_cert_pem"] = pem
            entry["pinned_fingerprint"] = fp
        config["pfsense"] = entry
    else:
        config.pop("pfsense", None)
    save_config(config)


def get_tofu_migrated() -> bool:
    """Return True if the one-shot verify_ssl→TOFU migration has already run."""
    return load_config().get("tofu_migrated", False)


def mark_tofu_migrated() -> None:
    config = load_config()
    config["tofu_migrated"] = True
    save_config(config)


def get_homeassistant_config() -> dict:
    return load_config().get("homeassistant", {})


def save_homeassistant_config(url: str, verify_ssl: bool = True) -> None:
    config = load_config()
    if url:
        config["homeassistant"] = {"url": url.rstrip("/"), "verify_ssl": verify_ssl}
    else:
        config.pop("homeassistant", None)
    save_config(config)


# ---------------------------------------------------------------------------
# Auto-update settings
# ---------------------------------------------------------------------------


def set_host_auto_update(
    slug: str,
    os_enabled: bool,
    os_schedule: str,
    auto_reboot: bool,
) -> None:
    config = load_config()
    for h in config.get("hosts", []):
        if slugify(h["name"]) == slug:
            if os_enabled:
                h["auto_update"] = {
                    "os_enabled": True,
                    "os_schedule": os_schedule,
                    "auto_reboot": auto_reboot,
                }
            else:
                h.pop("auto_update", None)
            break
    save_config(config)


def set_stack_auto_update(
    update_path: str,
    stack_name: str,
    enabled: bool,
    schedule: str,
) -> None:
    config = load_config()
    sau = config.setdefault("stack_auto_update", {})
    if enabled:
        sau[update_path] = {"enabled": True, "schedule": schedule, "name": stack_name}
    else:
        sau.pop(update_path, None)
    if not sau:
        config.pop("stack_auto_update", None)
    save_config(config)


def get_all_stack_auto_updates() -> dict:
    return load_config().get("stack_auto_update", {})


def get_available_ssh_keys() -> list[str]:
    """List key files in /app/keys/."""
    keys_dir = Path("/app/keys")
    if not keys_dir.exists():
        return []
    return sorted(
        f.name for f in keys_dir.iterdir() if f.is_file() and not f.name.startswith(".")
    )


def reset_config() -> None:
    """Remove all user-configured data (factory reset). Preserves the config file itself."""
    config = load_config()
    for key in (
        "hosts",
        "portainer",
        "dockerhub",
        "stack_auto_update",
        "proxmox",
        "proxmox_backup",
        "opnsense",
        "pfsense",
        "homeassistant",
    ):
        config.pop(key, None)
    save_config(config)



def get_update_check_config() -> dict:
    return load_config().get("update_check", {})


def save_update_check_config(cache_ttl_minutes: int) -> None:
    cfg = load_config()
    cfg["update_check"] = {"cache_ttl_minutes": int(cache_ttl_minutes)}
    save_config(cfg)


def get_update_check_ttl_minutes() -> int:
    """Return the configured TTL in minutes (default 15, 0 disables caching)."""
    try:
        val = int(get_update_check_config().get("cache_ttl_minutes", 15))
        return val if val >= 0 else 15
    except (ValueError, TypeError):
        return 15


def get_ssl_config() -> dict:
    return load_config().get("ssl", {})


def save_ssl_config(mode: str, hostname: str = "") -> None:
    config = load_config()
    config["ssl"] = {"mode": mode, "hostname": hostname}
    save_config(config)


def clear_ssl_config() -> None:
    config = load_config()
    config.pop("ssl", None)
    save_config(config)
