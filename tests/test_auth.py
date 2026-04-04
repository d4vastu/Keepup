"""Tests for authentication helpers (app/auth.py)."""
import pytest


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    """All tests in this module use a temp data dir."""


# ---------------------------------------------------------------------------
# create_admin
# ---------------------------------------------------------------------------

def test_create_admin_stores_username(data_dir):
    from app.auth import create_admin, get_admin_username
    create_admin(username="alice", password="password123", totp_secret=None)
    assert get_admin_username() == "alice"


def test_create_admin_stores_password_hash(data_dir):
    from app.auth import create_admin
    from app.credentials import get_integration_credentials
    create_admin(username="alice", password="password123", totp_secret=None)
    creds = get_integration_credentials("admin")
    assert "password_hash" in creds
    assert creds["password_hash"] != "password123"


def test_create_admin_stores_backup_key_hash(data_dir):
    from app.auth import create_admin
    from app.credentials import get_integration_credentials
    create_admin(username="alice", password="password123", totp_secret=None)
    creds = get_integration_credentials("admin")
    assert "backup_key_hash" in creds
    assert len(creds["backup_key_hash"]) == 64  # sha256 hex


def test_create_admin_returns_backup_key(data_dir):
    from app.auth import create_admin
    key = create_admin(username="alice", password="password123", totp_secret=None)
    assert len(key) == 35  # "XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX"
    assert key.count("-") == 3


def test_create_admin_without_totp(data_dir):
    from app.auth import create_admin
    from app.credentials import get_integration_credentials
    create_admin(username="alice", password="password123", totp_secret=None)
    creds = get_integration_credentials("admin")
    assert "totp_secret" not in creds


def test_create_admin_with_totp(data_dir):
    from app.auth import create_admin, new_totp_secret
    from app.credentials import get_integration_credentials
    secret = new_totp_secret()
    create_admin(username="alice", password="password123", totp_secret=secret)
    creds = get_integration_credentials("admin")
    assert creds.get("totp_secret") == secret


# ---------------------------------------------------------------------------
# get_admin_username
# ---------------------------------------------------------------------------

def test_get_admin_username_returns_stored(data_dir):
    from app.auth import create_admin, get_admin_username
    create_admin(username="bob", password="password123", totp_secret=None)
    assert get_admin_username() == "bob"


def test_get_admin_username_returns_empty_if_not_set(data_dir):
    from app.auth import get_admin_username
    assert get_admin_username() == ""


# ---------------------------------------------------------------------------
# verify_login
# ---------------------------------------------------------------------------

def test_verify_login_correct_credentials(data_dir):
    from app.auth import create_admin, verify_login
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_login("alice", "password123") is True


def test_verify_login_wrong_username(data_dir):
    from app.auth import create_admin, verify_login
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_login("bob", "password123") is False


def test_verify_login_wrong_password(data_dir):
    from app.auth import create_admin, verify_login
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_login("alice", "wrongpassword") is False


def test_verify_login_case_insensitive_username(data_dir):
    from app.auth import create_admin, verify_login
    create_admin(username="Alice", password="password123", totp_secret=None)
    assert verify_login("alice", "password123") is True
    assert verify_login("ALICE", "password123") is True


def test_verify_login_legacy_no_username_skips_check(data_dir):
    """If no username is stored (legacy account), username check is skipped."""
    from app.credentials import save_integration_credentials
    from app.auth import verify_login, _hash_password
    save_integration_credentials("admin", password_hash=_hash_password("password123"))
    # No username stored — any username (including empty) should pass
    assert verify_login("", "password123") is True
    assert verify_login("anything", "password123") is True


def test_verify_login_empty_store(data_dir):
    from app.auth import verify_login
    assert verify_login("alice", "password123") is False


# ---------------------------------------------------------------------------
# delete_admin
# ---------------------------------------------------------------------------

def test_delete_admin_removes_account(data_dir):
    from app.auth import create_admin, delete_admin, admin_exists
    create_admin(username="alice", password="password123", totp_secret=None)
    assert admin_exists() is True
    delete_admin()
    assert admin_exists() is False


def test_admin_exists_after_delete_returns_false(data_dir):
    from app.auth import create_admin, delete_admin, admin_exists
    create_admin(username="alice", password="password123", totp_secret=None)
    delete_admin()
    assert admin_exists() is False


# ---------------------------------------------------------------------------
# verify_password (existing function)
# ---------------------------------------------------------------------------

def test_verify_password_correct(data_dir):
    from app.auth import create_admin, verify_password
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_password("password123") is True


def test_verify_password_wrong(data_dir):
    from app.auth import create_admin, verify_password
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_password("wrongpass") is False


def test_verify_password_no_account(data_dir):
    from app.auth import verify_password
    assert verify_password("anything") is False


# ---------------------------------------------------------------------------
# verify_backup_key
# ---------------------------------------------------------------------------

def test_verify_backup_key_round_trip(data_dir):
    from app.auth import create_admin, verify_backup_key
    backup_key = create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_backup_key(backup_key) is True


def test_verify_backup_key_wrong(data_dir):
    from app.auth import create_admin, verify_backup_key
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_backup_key("WRONG-WRONG-WRONG-WRONGKEY") is False


def test_verify_backup_key_case_insensitive(data_dir):
    from app.auth import create_admin, verify_backup_key
    backup_key = create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_backup_key(backup_key.lower()) is True


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------

def test_change_password_updates_hash(data_dir):
    from app.auth import create_admin, change_password, verify_password
    create_admin(username="alice", password="oldpassword", totp_secret=None)
    change_password("newpassword123")
    assert verify_password("newpassword123") is True
    assert verify_password("oldpassword") is False


# ---------------------------------------------------------------------------
# mfa_enrolled / enroll_mfa / remove_mfa
# ---------------------------------------------------------------------------

def test_mfa_enrolled_false_before_enrollment(data_dir):
    from app.auth import create_admin, mfa_enrolled
    create_admin(username="alice", password="password123", totp_secret=None)
    assert mfa_enrolled() is False


def test_mfa_enrolled_true_after_enrollment(data_dir):
    from app.auth import create_admin, enroll_mfa, mfa_enrolled, new_totp_secret
    create_admin(username="alice", password="password123", totp_secret=None)
    enroll_mfa(new_totp_secret())
    assert mfa_enrolled() is True


def test_remove_mfa_clears_totp(data_dir):
    from app.auth import create_admin, enroll_mfa, remove_mfa, mfa_enrolled, new_totp_secret
    create_admin(username="alice", password="password123", totp_secret=None)
    enroll_mfa(new_totp_secret())
    assert mfa_enrolled() is True
    remove_mfa()
    assert mfa_enrolled() is False


# ---------------------------------------------------------------------------
# regenerate_backup_key
# ---------------------------------------------------------------------------

def test_regenerate_backup_key_returns_new_key(data_dir):
    from app.auth import create_admin, regenerate_backup_key, verify_backup_key
    old_key = create_admin(username="alice", password="password123", totp_secret=None)
    new_key = regenerate_backup_key()
    assert new_key != old_key
    assert verify_backup_key(new_key) is True


def test_regenerate_backup_key_invalidates_old(data_dir):
    from app.auth import create_admin, regenerate_backup_key, verify_backup_key
    old_key = create_admin(username="alice", password="password123", totp_secret=None)
    regenerate_backup_key()
    assert verify_backup_key(old_key) is False


# ---------------------------------------------------------------------------
# _verify_password exception path
# ---------------------------------------------------------------------------

def test_verify_password_handles_exception(data_dir):
    """_verify_password returns False when bcrypt raises."""
    from app.auth import _verify_password
    # Passing an invalid hash should return False, not raise
    assert _verify_password("password", "not-a-valid-hash") is False


# ---------------------------------------------------------------------------
# verify_totp
# ---------------------------------------------------------------------------

def test_verify_totp_no_secret_returns_false(data_dir):
    """verify_totp returns False when no TOTP secret is configured."""
    from app.auth import create_admin, verify_totp
    create_admin(username="alice", password="password123", totp_secret=None)
    assert verify_totp("123456") is False


# ---------------------------------------------------------------------------
# reset_password_with_backup_key
# ---------------------------------------------------------------------------

def test_reset_password_with_valid_backup_key(data_dir):
    from app.auth import create_admin, reset_password_with_backup_key, verify_login
    backup_key = create_admin(username="alice", password="password123", totp_secret=None)
    result = reset_password_with_backup_key(backup_key, "newpassword456")
    assert result is True
    assert verify_login("alice", "newpassword456") is True


def test_reset_password_with_invalid_backup_key(data_dir):
    from app.auth import create_admin, reset_password_with_backup_key
    create_admin(username="alice", password="password123", totp_secret=None)
    result = reset_password_with_backup_key("WRONG-WRONG-WRONG-WRONG", "newpassword456")
    assert result is False
