"""
Encrypted credential store.

Credentials (SSH password, SSH private key, sudo password) are stored in a
Fernet-encrypted JSON file at /app/data/credentials.json. The encryption key is
resolved in order: the KEEPUP_SECRET_KEY env var, then the file named by
KEEPUP_SECRET_KEY_FILE (e.g. a Docker/k8s secret), then an auto-generated
/app/data/.secret. The first two keep the key out of the data volume so it does
not travel in volume backups; only the .secret fallback is auto-generated.

Nothing sensitive is ever written to config.yml.
"""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_SECRET_FILE = _DATA_DIR / ".secret"
_CREDS_FILE = _DATA_DIR / "credentials.json"

# SSH private keys live as bare filenames under this directory; callers store
# only the filename and resolve it to an absolute path via resolve_key_path.
_KEYS_DIR = Path(os.getenv("KEYS_PATH", "/app/keys"))


def resolve_key_path(name: str) -> str:
    """Resolve an SSH key filename to an absolute path inside the keys dir.

    The stored SSH/Proxmox key is a bare filename under the keys directory;
    this maps it to the absolute path asyncssh needs while rejecting path
    traversal — absolute paths, ``..``, or anything resolving outside the dir.
    Single source of truth shared by the dashboard routes, the Proxmox LXC
    upgrade paths, and the Docker backend so the guard cannot drift.
    """
    if ".." in name or Path(name).is_absolute():
        raise ValueError(f"SSH key path escapes keys directory: {name!r}")
    resolved = (_KEYS_DIR / name).resolve()
    if not resolved.is_relative_to(_KEYS_DIR.resolve()):
        raise ValueError(f"SSH key path escapes keys directory: {name!r}")
    return str(resolved)


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _validate_key(key: bytes, source: str) -> bytes:
    """Return ``key`` if it is a valid Fernet key, else raise a clear error.

    Naming the offending ``source`` turns a config typo into an actionable
    startup error instead of a silent fall-through to a freshly generated key,
    which would orphan the existing credential store.
    """
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{source} is not a valid Fernet key (expected 32 url-safe "
            f"base64-encoded bytes): {exc}"
        ) from exc
    return key


def _resolve_key_bytes() -> bytes:
    """Resolve the Fernet key, preferring sources outside the data volume.

    Order: ``KEEPUP_SECRET_KEY`` env var, then ``KEEPUP_SECRET_KEY_FILE`` path
    (e.g. a Docker/Podman/k8s secret under ``/run/secrets/...``), then the
    auto-generated ``.secret`` in the data dir. Only the on-disk fallback is
    ever generated, so an operator-supplied key is never written into the data
    volume (and therefore never lands in a backup of it).
    """
    env_key = os.getenv("KEEPUP_SECRET_KEY")
    if env_key:
        return _validate_key(env_key.encode(), "KEEPUP_SECRET_KEY")

    key_file = os.getenv("KEEPUP_SECRET_KEY_FILE")
    if key_file:
        try:
            raw = Path(key_file).read_bytes()
        except OSError as exc:
            raise ValueError(
                f"KEEPUP_SECRET_KEY_FILE={key_file!r} could not be read: {exc}"
            ) from exc
        return _validate_key(raw.strip(), "KEEPUP_SECRET_KEY_FILE")

    _ensure_data_dir()
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_bytes(Fernet.generate_key())
        _SECRET_FILE.chmod(0o600)
    return _SECRET_FILE.read_bytes()


def _get_fernet() -> Fernet:
    return Fernet(_resolve_key_bytes())


def _load_store() -> dict:
    if not _CREDS_FILE.exists():
        return {}
    try:
        raw = _get_fernet().decrypt(_CREDS_FILE.read_bytes())
        return json.loads(raw.decode())
    except (InvalidToken, Exception):
        return {}


def _save_store(store: dict) -> None:
    _ensure_data_dir()
    encrypted = _get_fernet().encrypt(json.dumps(store).encode())
    _CREDS_FILE.write_bytes(encrypted)
    _CREDS_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_credentials(slug: str) -> dict:
    """Return the credential dict for a host slug. Never raises."""
    return _load_store().get(slug, {})


def save_credentials(
    slug: str,
    ssh_password: str | None = None,
    ssh_key: str | None = None,
    sudo_password: str | None = None,
) -> None:
    """
    Save credentials for a host. Pass None to leave an existing value
    unchanged; pass an empty string "" to explicitly clear a field.
    """
    store = _load_store()
    entry = store.get(slug, {})

    for field, value in [
        ("ssh_password", ssh_password),
        ("ssh_key", ssh_key),
        ("sudo_password", sudo_password),
    ]:
        if value is None:
            continue  # don't touch existing value
        if value == "":
            entry.pop(field, None)  # clear
        else:
            entry[field] = value  # set

    store[slug] = entry
    _save_store(store)


def save_sudo_password(slug: str, password: str) -> None:
    save_credentials(slug, sudo_password=password)


def delete_credentials(slug: str) -> None:
    store = _load_store()
    store.pop(slug, None)
    _save_store(store)


def rename_credentials(old_slug: str, new_slug: str) -> None:
    """Called when a host is renamed so credentials follow the new slug."""
    if old_slug == new_slug:
        return
    store = _load_store()
    if old_slug in store:
        store[new_slug] = store.pop(old_slug)
        _save_store(store)


# ---------------------------------------------------------------------------
# Integration credentials (Portainer, DockerHub, etc.)
# ---------------------------------------------------------------------------


def get_integration_credentials(key: str) -> dict:
    """Return stored credentials for a named integration. Never raises."""
    return _load_store().get(f"__integration_{key}__", {})


def save_integration_credentials(key: str, **fields) -> None:
    """
    Save credentials for a named integration.
    Pass None to leave an existing value unchanged; "" to clear it.
    """
    store = _load_store()
    entry = store.get(f"__integration_{key}__", {})
    for field, value in fields.items():
        if value is None:
            continue
        if value == "":
            entry.pop(field, None)
        else:
            entry[field] = value
    store[f"__integration_{key}__"] = entry
    _save_store(store)


def delete_integration_credentials(key: str) -> None:
    """Remove an integration credential entry entirely."""
    store = _load_store()
    store.pop(f"__integration_{key}__", None)
    _save_store(store)


def wipe_credential_store() -> None:
    """Erase all credentials (factory reset)."""
    _save_store({})


def credential_status(slug: str) -> dict:
    """Returns which credentials are configured for a host (no secrets exposed)."""
    creds = get_credentials(slug)
    return {
        "has_ssh_password": bool(creds.get("ssh_password")),
        "has_ssh_key": bool(creds.get("ssh_key")),
        "has_sudo_password": bool(creds.get("sudo_password")),
    }
