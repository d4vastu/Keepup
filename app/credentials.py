"""
Encrypted credential store.

Credentials (SSH password, SSH private key, sudo password) are stored in a
Fernet-encrypted JSON file at /app/data/credentials.json. The encryption key
is auto-generated on first run and stored at /app/data/.secret.

Nothing sensitive is ever written to config.yml.
"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_SECRET_FILE = _DATA_DIR / ".secret"
_CREDS_FILE = _DATA_DIR / "credentials.json"


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_fernet() -> Fernet:
    _ensure_data_dir()
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_bytes(Fernet.generate_key())
        _SECRET_FILE.chmod(0o600)
    return Fernet(_SECRET_FILE.read_bytes())


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
            continue           # don't touch existing value
        if value == "":
            entry.pop(field, None)   # clear
        else:
            entry[field] = value     # set

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
