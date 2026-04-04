"""Tests for the encrypted credential store."""
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    """All tests in this module use a temp data dir."""


# ---------------------------------------------------------------------------
# Key auto-generation
# ---------------------------------------------------------------------------

def test_fernet_key_auto_created(data_dir):
    from app.credentials import _get_fernet, _SECRET_FILE
    _get_fernet()
    assert _SECRET_FILE.exists()


def test_fernet_key_is_stable(data_dir):
    from app.credentials import _get_fernet
    f1 = _get_fernet()
    f2 = _get_fernet()
    # Encrypting with one and decrypting with the other must work
    token = f1.encrypt(b"hello")
    assert f2.decrypt(token) == b"hello"


# ---------------------------------------------------------------------------
# save / get round-trip
# ---------------------------------------------------------------------------

def test_save_and_get_ssh_password(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("myhost", ssh_password="s3cr3t")
    creds = get_credentials("myhost")
    assert creds["ssh_password"] == "s3cr3t"


def test_save_and_get_ssh_key(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("myhost", ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----\n...")
    creds = get_credentials("myhost")
    assert creds["ssh_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_save_and_get_sudo_password(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("myhost", sudo_password="sudopass")
    creds = get_credentials("myhost")
    assert creds["sudo_password"] == "sudopass"


def test_none_does_not_overwrite_existing(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("myhost", ssh_password="original")
    save_credentials("myhost", ssh_password=None, sudo_password="newsudo")
    creds = get_credentials("myhost")
    assert creds["ssh_password"] == "original"
    assert creds["sudo_password"] == "newsudo"


def test_empty_string_clears_field(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("myhost", ssh_password="original")
    save_credentials("myhost", ssh_password="")
    creds = get_credentials("myhost")
    assert "ssh_password" not in creds


def test_get_credentials_unknown_host_returns_empty(data_dir):
    from app.credentials import get_credentials
    assert get_credentials("nonexistent") == {}


def test_multiple_hosts_isolated(data_dir):
    from app.credentials import save_credentials, get_credentials
    save_credentials("host-a", ssh_password="aaa")
    save_credentials("host-b", ssh_password="bbb")
    assert get_credentials("host-a")["ssh_password"] == "aaa"
    assert get_credentials("host-b")["ssh_password"] == "bbb"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_credentials(data_dir):
    from app.credentials import save_credentials, get_credentials, delete_credentials
    save_credentials("myhost", ssh_password="s3cr3t")
    delete_credentials("myhost")
    assert get_credentials("myhost") == {}


def test_delete_nonexistent_is_safe(data_dir):
    from app.credentials import delete_credentials
    delete_credentials("does-not-exist")  # should not raise


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

def test_rename_credentials(data_dir):
    from app.credentials import save_credentials, get_credentials, rename_credentials
    save_credentials("old-slug", ssh_password="pass")
    rename_credentials("old-slug", "new-slug")
    assert get_credentials("new-slug")["ssh_password"] == "pass"
    assert get_credentials("old-slug") == {}


def test_rename_same_slug_is_noop(data_dir):
    from app.credentials import save_credentials, get_credentials, rename_credentials
    save_credentials("myhost", ssh_password="pass")
    rename_credentials("myhost", "myhost")
    assert get_credentials("myhost")["ssh_password"] == "pass"


def test_rename_nonexistent_is_safe(data_dir):
    from app.credentials import rename_credentials
    rename_credentials("ghost", "new-ghost")  # should not raise


# ---------------------------------------------------------------------------
# save_sudo_password shortcut
# ---------------------------------------------------------------------------

def test_save_sudo_password(data_dir):
    from app.credentials import save_sudo_password, get_credentials
    save_sudo_password("myhost", "sudopass123")
    assert get_credentials("myhost")["sudo_password"] == "sudopass123"


# ---------------------------------------------------------------------------
# credential_status
# ---------------------------------------------------------------------------

def test_credential_status_empty(data_dir):
    from app.credentials import credential_status
    status = credential_status("myhost")
    assert status == {"has_ssh_password": False, "has_ssh_key": False, "has_sudo_password": False}


def test_credential_status_with_password(data_dir):
    from app.credentials import save_credentials, credential_status
    save_credentials("myhost", ssh_password="pass")
    status = credential_status("myhost")
    assert status["has_ssh_password"] is True
    assert status["has_ssh_key"] is False
    assert status["has_sudo_password"] is False


def test_credential_status_with_key_and_sudo(data_dir):
    from app.credentials import save_credentials, credential_status
    save_credentials("myhost", ssh_key="-----BEGIN...", sudo_password="sudo")
    status = credential_status("myhost")
    assert status["has_ssh_key"] is True
    assert status["has_sudo_password"] is True
    assert status["has_ssh_password"] is False


# ---------------------------------------------------------------------------
# Encryption: credentials.json is not plaintext
# ---------------------------------------------------------------------------

def test_credentials_file_is_not_plaintext(data_dir):
    from app.credentials import save_credentials, _CREDS_FILE
    save_credentials("myhost", ssh_password="supersecret")
    raw = _CREDS_FILE.read_bytes()
    assert b"supersecret" not in raw
    assert b"myhost" not in raw
