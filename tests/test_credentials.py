"""Tests for the encrypted credential store."""

import pytest


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir, monkeypatch):
    """All tests use a temp data dir and a clean encryption-key environment."""
    monkeypatch.delenv("KEEPUP_SECRET_KEY", raising=False)
    monkeypatch.delenv("KEEPUP_SECRET_KEY_FILE", raising=False)


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
# Externalizable encryption key (OP#203)
# ---------------------------------------------------------------------------


def test_env_var_key_is_used_and_no_secret_written(monkeypatch):
    from cryptography.fernet import Fernet

    from app.credentials import _SECRET_FILE, _get_fernet

    key = Fernet.generate_key()
    monkeypatch.setenv("KEEPUP_SECRET_KEY", key.decode())

    token = Fernet(key).encrypt(b"hello")
    assert _get_fernet().decrypt(token) == b"hello"
    # An operator-supplied key must never be written into the data volume.
    assert not _SECRET_FILE.exists()


def test_key_file_is_used_and_no_secret_written(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from app.credentials import _SECRET_FILE, _get_fernet

    key = Fernet.generate_key()
    key_file = tmp_path / "keepup_secret_key"
    key_file.write_bytes(key + b"\n")  # trailing newline must be tolerated
    monkeypatch.setenv("KEEPUP_SECRET_KEY_FILE", str(key_file))

    token = Fernet(key).encrypt(b"hello")
    assert _get_fernet().decrypt(token) == b"hello"
    assert not _SECRET_FILE.exists()


def test_env_var_takes_precedence_over_key_file(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from app.credentials import _get_fernet

    env_key = Fernet.generate_key()
    file_key = Fernet.generate_key()
    key_file = tmp_path / "k"
    key_file.write_bytes(file_key)
    monkeypatch.setenv("KEEPUP_SECRET_KEY", env_key.decode())
    monkeypatch.setenv("KEEPUP_SECRET_KEY_FILE", str(key_file))

    token = Fernet(env_key).encrypt(b"x")
    assert _get_fernet().decrypt(token) == b"x"


def test_key_file_takes_precedence_over_on_disk_secret(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from app.credentials import _SECRET_FILE, _get_fernet

    # An on-disk .secret exists, but a configured key file must win.
    _SECRET_FILE.write_bytes(Fernet.generate_key())
    file_key = Fernet.generate_key()
    key_file = tmp_path / "k"
    key_file.write_bytes(file_key)
    monkeypatch.setenv("KEEPUP_SECRET_KEY_FILE", str(key_file))

    token = Fernet(file_key).encrypt(b"x")
    assert _get_fernet().decrypt(token) == b"x"


def test_invalid_env_key_raises_and_does_not_write_secret(monkeypatch):
    from app.credentials import _SECRET_FILE, _get_fernet

    monkeypatch.setenv("KEEPUP_SECRET_KEY", "not-a-valid-fernet-key")

    with pytest.raises(ValueError):
        _get_fernet()
    # Must not silently fall back to a freshly generated key.
    assert not _SECRET_FILE.exists()


def test_missing_key_file_path_raises(monkeypatch, tmp_path):
    from app.credentials import _get_fernet

    monkeypatch.setenv("KEEPUP_SECRET_KEY_FILE", str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError):
        _get_fernet()


def test_falls_back_to_on_disk_secret_without_env():
    from app.credentials import _SECRET_FILE, _get_fernet

    # No env vars set (cleared by the autouse fixture) -> generate/read .secret.
    _get_fernet()
    assert _SECRET_FILE.exists()


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
    assert status == {
        "has_ssh_password": False,
        "has_ssh_key": False,
        "has_sudo_password": False,
    }


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


# ---------------------------------------------------------------------------
# Integration credentials — delete and wipe (PR1 additions)
# ---------------------------------------------------------------------------


def test_delete_integration_credentials_removes_entry(data_dir):
    from app.credentials import (
        save_integration_credentials,
        get_integration_credentials,
        delete_integration_credentials,
    )

    save_integration_credentials("portainer", api_key="mykey")
    assert get_integration_credentials("portainer").get("api_key") == "mykey"
    delete_integration_credentials("portainer")
    assert get_integration_credentials("portainer") == {}


def test_delete_integration_credentials_safe_for_nonexistent(data_dir):
    from app.credentials import delete_integration_credentials

    # Should not raise even if key doesn't exist
    delete_integration_credentials("nonexistent-key")


def test_wipe_credential_store_clears_everything(data_dir):
    from app.credentials import (
        save_credentials,
        save_integration_credentials,
        wipe_credential_store,
        get_credentials,
        get_integration_credentials,
    )

    save_credentials("myhost", ssh_password="pass")
    save_integration_credentials("portainer", api_key="key")
    wipe_credential_store()
    assert get_credentials("myhost") == {}
    assert get_integration_credentials("portainer") == {}


def test_wipe_credential_store_then_get_returns_empty(data_dir):
    from app.credentials import save_credentials, wipe_credential_store, get_credentials

    save_credentials("host-a", ssh_password="aaa")
    save_credentials("host-b", ssh_password="bbb")
    wipe_credential_store()
    assert get_credentials("host-a") == {}
    assert get_credentials("host-b") == {}


# ---------------------------------------------------------------------------
# resolve_key_path — shared SSH key path resolver + traversal guard (OP#182)
# ---------------------------------------------------------------------------


def test_resolve_key_path_maps_filename_into_keys_dir(monkeypatch, tmp_path):
    from app import credentials

    monkeypatch.setattr(credentials, "_KEYS_DIR", tmp_path)
    assert credentials.resolve_key_path("id_ed25519") == str(tmp_path / "id_ed25519")


def test_resolve_key_path_rejects_traversal(monkeypatch, tmp_path):
    from app import credentials

    monkeypatch.setattr(credentials, "_KEYS_DIR", tmp_path)
    with pytest.raises(ValueError, match="escapes keys directory"):
        credentials.resolve_key_path("../../etc/passwd")
