"""
Authentication helpers.

Admin account stored in the encrypted credential store under __admin__:
  {
    "username":            str,           # display/login username
    "password_hash":       str,           # bcrypt
    "totp_secret":         str | None,    # base32, None = MFA not enrolled
    "backup_key_hash":     str,           # sha256 hex of the backup key
    "password_meets_policy": bool,        # True once a ≥12-char password is saved
  }
"""

import hashlib
import os
import secrets
from pathlib import Path

import bcrypt as _bcrypt
import pyotp

from .credentials import (
    delete_integration_credentials,
    get_integration_credentials,
    save_integration_credentials,
)


# Minimum password length per NIST SP 800-63B.  Raised from 8 to 12 chars so
# that existing credentials set under the old policy can be identified and
# the user prompted to upgrade on next login (see "password_meets_policy" flag).
_MIN_PASSWORD_LEN = 12


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(12)).decode()


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))
_SESSION_SECRET_FILE = _DATA_DIR / ".session_secret"

# ---------------------------------------------------------------------------
# Session secret
# ---------------------------------------------------------------------------


def get_session_secret() -> str:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _SESSION_SECRET_FILE.exists():
        _SESSION_SECRET_FILE.write_text(secrets.token_hex(32))
        _SESSION_SECRET_FILE.chmod(0o600)
    return _SESSION_SECRET_FILE.read_text().strip()


# ---------------------------------------------------------------------------
# Account existence
# ---------------------------------------------------------------------------


def admin_exists() -> bool:
    return bool(get_integration_credentials("admin").get("password_hash"))


def mfa_enrolled() -> bool:
    return bool(get_integration_credentials("admin").get("totp_secret"))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name="admin", issuer_name="Keepup")


def _generate_backup_key() -> str:
    """40-char hex key in 4 groups of 8: A3F29B1C-E7D42F8A-B5C19E3D-2A4F8C1E"""
    raw = secrets.token_hex(16).upper()  # 32 hex chars
    return "-".join(raw[i : i + 8] for i in range(0, 32, 8))


def _hash_backup_key(key: str) -> str:
    return hashlib.sha256(key.strip().upper().encode()).hexdigest()


def create_admin(username: str, password: str, totp_secret: str | None) -> str:
    """
    Persist the admin account. Returns the plaintext backup key (show once).
    totp_secret is None if user skipped MFA enrollment.

    Sets ``password_meets_policy = True`` when the password is ≥ _MIN_PASSWORD_LEN
    characters so the home-page notice is suppressed for new accounts.
    """
    backup_key = _generate_backup_key()
    data: dict = {
        "username": username,
        "password_hash": _hash_password(password),
        "backup_key_hash": _hash_backup_key(backup_key),
        "password_meets_policy": len(password) >= _MIN_PASSWORD_LEN,
    }
    if totp_secret:
        data["totp_secret"] = totp_secret
    save_integration_credentials("admin", **data)
    return backup_key


def get_admin_username() -> str:
    """Return the stored admin username, or empty string if not set."""
    return get_integration_credentials("admin").get("username", "")


def verify_login(username: str, password: str) -> bool:
    """Check username (case-insensitive, skipped for legacy accounts) then password."""
    creds = get_integration_credentials("admin")
    stored_username = creds.get("username", "")
    if stored_username and username.strip().lower() != stored_username.strip().lower():
        return False
    h = creds.get("password_hash", "")
    return bool(h) and _verify_password(password, h)


def delete_admin() -> None:
    """Remove the admin account entirely."""
    delete_integration_credentials("admin")


# ---------------------------------------------------------------------------
# Login verification
# ---------------------------------------------------------------------------


def verify_password(password: str) -> bool:
    h = get_integration_credentials("admin").get("password_hash", "")
    return bool(h) and _verify_password(password, h)


def verify_totp(code: str) -> bool:
    secret = get_integration_credentials("admin").get("totp_secret", "")
    if not secret:
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


# ---------------------------------------------------------------------------
# Backup key
# ---------------------------------------------------------------------------


def verify_backup_key(key: str) -> bool:
    stored = get_integration_credentials("admin").get("backup_key_hash", "")
    return bool(stored) and _hash_backup_key(key) == stored


# ---------------------------------------------------------------------------
# Account mutations (all require prior auth)
# ---------------------------------------------------------------------------


def change_password(new_password: str) -> None:
    """Update the admin password hash and record whether the new password meets policy."""
    save_integration_credentials(
        "admin",
        password_hash=_hash_password(new_password),
        # Record that the saved password meets the current minimum length policy so
        # the home-page upgrade notice is suppressed after the user has updated.
        password_meets_policy=len(new_password) >= _MIN_PASSWORD_LEN,
    )


def enroll_mfa(totp_secret: str) -> None:
    save_integration_credentials("admin", totp_secret=totp_secret)


def remove_mfa() -> None:
    creds = get_integration_credentials("admin")
    creds.pop("totp_secret", None)
    # Re-save without the key by clearing it
    save_integration_credentials("admin", totp_secret="")


def regenerate_backup_key() -> str:
    """Returns new plaintext backup key."""
    backup_key = _generate_backup_key()
    save_integration_credentials("admin", backup_key_hash=_hash_backup_key(backup_key))
    return backup_key


def reset_password_with_backup_key(backup_key: str, new_password: str) -> bool:
    """Password reset flow for locked-out users. Returns False if key invalid."""
    if not verify_backup_key(backup_key):
        return False
    change_password(new_password)
    return True
